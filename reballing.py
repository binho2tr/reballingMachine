#!/usr/bin/env python3
"""
Máquina de Reballing — Controlador completo
Hardware:
  Base:    MAX6675 (CS=GPIO8)  + SSR Fotek 25DA (GPIO18)
  Canhão:  MAX6675 (CS=GPIO7)  + SSR (GPIO24)
  Vent.:   Relé (GPIO25)
  SPI:     SCK=GPIO11, MISO=GPIO9

Acesso: http://<IP-do-Rasp>:5000
"""

import spidev
import RPi.GPIO as GPIO
import time
import csv
import threading
import os
import json
from datetime import datetime
from flask import Flask, Response, render_template_string, request

# ═══════════════════════════════════════════════════════
# PINOS
# ═══════════════════════════════════════════════════════
SSR_BASE_PIN = 18
SSR_GUN_PIN  = 24
FAN_PIN      = 25
BUZZER_PIN   = 16

CS_BASE = 0
CS_GUN  = 1

# Nota: o display ILI9488 foi migrado para um ESP32 externo,
# que consulta a rota /display_data via HTTP. Veja o projeto
# reballing-display/ (PlatformIO) para o firmware do display.

# ═══════════════════════════════════════════════════════
# PID — Base
# ═══════════════════════════════════════════════════════
BASE_KP = 1.5
BASE_KI = 0.01
BASE_KD = 1.0

# ═══════════════════════════════════════════════════════
# PID — Canhão
# KP baixo evita oscilação — canhão tem grande atraso de resposta
# KD alto amortece as oscilações
# ═══════════════════════════════════════════════════════
GUN_KP = 0.8
GUN_KI = 0.005
GUN_KD = 2.0

# ═══════════════════════════════════════════════════════
# PARÂMETROS GERAIS
# ═══════════════════════════════════════════════════════
WINDOW_SIZE  = 2.0
MAX_TEMP     = 260
MAX_INTEGRAL = 50

CHIP_REMOVE_TEMP = 210

FAN_ON_TEMP  = 150
FAN_OFF_TEMP =  45   # desliga em 45°C — histerese de 5°C evita oscilação do relé

RAMP_RATE = 2.0   # °C/s — rampa suavizada no Pre-heat

# Tempo mínimo que o canhão fica ligado a cada pulso bang-bang
# Garante que o ar quente tem tempo de fluir até a placa (~5cm)
GUN_MIN_ON_S = 3.0   # segundos

# Lógica do relé do ventilador:
# False = ativo em LOW  (módulos de relé tipo Arduino — padrão)
# True  = ativo em HIGH (SSR, MOSFET)
FAN_ACTIVE_HIGH = False

# ═══════════════════════════════════════════════════════
# LOG
# ═══════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR     = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════
# PERFIS
# ═══════════════════════════════════════════════════════
PROFILES = {
    "generico": {
        "label":  "Genérico",
        "desc":   "Perfil padrão para chips SMD comuns",
        "group":  "Geral",
        "gun_sp": 210,
        "steps": [
            ("Pre-heat",  80,  120),
            ("Aquec.",   150,  180),
            ("Soak",     150,   60),
            ("Reflow",   220,  120),
            ("Resfria",   50,    0),
        ],
    },
    "xbox360_ksb": {
        "label":  "KSB — GPU/CPU",
        "desc":   "Chip grande, massa térmica alta",
        "group":  "Xbox 360",
        "gun_sp": 235,
        "steps": [
            ("Pre-heat",  75,  150),
            ("Aquec.",   135,  210),
            ("Soak",     135,  120),
            ("Reflow",   215,  150),
            ("Resfria",   50,    0),
        ],
    },
    "xbox360_apu_ref": {
        "label":  "APU — Ref. Profissional",
        "desc":   "Perfil referência para remoção de APU",
        "group":  "Xbox 360",
        "gun_sp": 210,
        "steps": [
            ("Pre-heat", 105,   90),
            ("Soak",     155,   75),
            ("Reflow",   220,   45),
            ("Resfria",   50,    0),
        ],
    },
    "xbox360_memoria": {
        "label":  "Memória / NAND",
        "desc":   "RAM DDR, NAND Flash — chip pequeno, substrate fino",
        "group":  "Xbox 360",
        "gun_sp": 230,   # baixou 240→230 — pico de 354°C era alto demais
        "steps": [
            ("Pre-heat",  70,   90),
            ("Aquec.",   140,  120),
            ("Soak",     140,   60),
            ("Reflow",   210,  120),   # subiu 90→120s — só 22s acima de 205°C no run anterior
            ("Resfria",   50,    0),
        ],
    },
    "ps3_rsx": {
        "label":  "RSX / Cell",
        "desc":   "GPU RSX e Cell do PS3, substrate frágil",
        "group":  "PS3",
        "gun_sp": 195,
        "steps": [
            ("Pre-heat",  60,  180),
            ("Aquec.",   130,  240),
            ("Soak",     130,  120),
            ("Reflow",   210,  100),
            ("Resfria",   50,    0),
        ],
    },
    "gpu_laptop": {
        "label":  "GPU Laptop",
        "desc":   "GPUs BGA de notebook (nVidia, AMD)",
        "group":  "Notebook",
        "gun_sp": 205,
        "steps": [
            ("Pre-heat",  80,  120),
            ("Aquec.",   140,  180),
            ("Soak",     140,   90),
            ("Reflow",   215,  110),
            ("Resfria",   50,    0),
        ],
    },
    "chipset": {
        "label":  "Chipset / Northbridge",
        "desc":   "Chipsets Intel/AMD, chips menores",
        "group":  "Notebook",
        "gun_sp": 215,
        "steps": [
            ("Pre-heat",  80,  100),
            ("Aquec.",   150,  150),
            ("Soak",     150,   60),
            ("Reflow",   225,  100),
            ("Resfria",   50,    0),
        ],
    },
}

# ═══════════════════════════════════════════════════════
# PERFIS CUSTOMIZADOS — criados pelo usuário via interface web
# Armazenados como JSON individual em CUSTOM_PROFILES_DIR.
# Mesclados com os PROFILES embutidos acima na inicialização
# e sempre que um perfil é criado/editado/excluído.
# ═══════════════════════════════════════════════════════
CUSTOM_PROFILES_DIR = os.path.join(SCRIPT_DIR, "custom_profiles")
os.makedirs(CUSTOM_PROFILES_DIR, exist_ok=True)

BUILTIN_PROFILE_KEYS = set(PROFILES.keys())   # nunca sobrescritos/excluídos pela API


def _custom_profile_path(key):
    """Monta o caminho do arquivo, validando a key contra path traversal."""
    safe_key = "".join(c for c in key if c.isalnum() or c in "_-")
    if not safe_key:
        return None
    return os.path.join(CUSTOM_PROFILES_DIR, f"{safe_key}.json")


def load_custom_profiles():
    """Lê todos os JSONs de CUSTOM_PROFILES_DIR e mescla em PROFILES."""
    if not os.path.isdir(CUSTOM_PROFILES_DIR):
        return
    for fname in sorted(os.listdir(CUSTOM_PROFILES_DIR)):
        if not fname.endswith(".json"):
            continue
        key = fname[:-5]
        if key in BUILTIN_PROFILE_KEYS:
            continue   # nunca sobrescreve um perfil embutido
        try:
            with open(os.path.join(CUSTOM_PROFILES_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = [(s["name"], int(s["sp"]), int(s["dur"])) for s in data["steps"]]
            PROFILES[key] = {
                "label":  data["label"],
                "desc":   data.get("desc", ""),
                "group":  data.get("group", "Customizado"),
                "gun_sp": int(data.get("gun_sp", 0)),
                "steps":  steps,
                "custom": True,
            }
        except Exception as e:
            print(f"Aviso: falha ao carregar perfil customizado '{fname}': {e}")


def save_custom_profile(key, label, desc, group, gun_sp, steps):
    """Salva/atualiza um perfil customizado em disco e em memória."""
    path = _custom_profile_path(key)
    if path is None:
        raise ValueError("Chave de perfil inválida")
    if key in BUILTIN_PROFILE_KEYS:
        raise ValueError("Não é possível sobrescrever um perfil embutido")

    data = {
        "label":  label,
        "desc":   desc,
        "group":  group or "Customizado",
        "gun_sp": int(gun_sp),
        "steps":  [{"name": s[0], "sp": int(s[1]), "dur": int(s[2])} for s in steps],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    PROFILES[key] = {
        "label":  label,
        "desc":   desc,
        "group":  group or "Customizado",
        "gun_sp": int(gun_sp),
        "steps":  list(steps),
        "custom": True,
    }


def delete_custom_profile(key):
    """Remove um perfil customizado do disco e da memória."""
    if key in BUILTIN_PROFILE_KEYS:
        raise ValueError("Não é possível excluir um perfil embutido")
    path = _custom_profile_path(key)
    if path is None or not os.path.exists(path):
        raise ValueError("Perfil não encontrado")
    os.remove(path)
    PROFILES.pop(key, None)


load_custom_profiles()

PROFILE_KEYS = list(PROFILES.keys())


# ═══════════════════════════════════════════════════════
# ESTADO COMPARTILHADO
# ═══════════════════════════════════════════════════════
class SharedState:
    def __init__(self):
        self.lock           = threading.Lock()
        self.times          = []
        self.base_temps     = []
        self.gun_temps      = []
        self.setpoints      = []
        self.base_duties    = []
        self.gun_duties     = []
        self.step_name      = "Aguardando..."
        self.step_idx       = 0
        self.cur_base_temp  = 0.0
        self.cur_gun_temp   = 0.0
        self.cur_base_sp    = 0.0
        self.cur_base_duty  = 0.0
        self.cur_gun_duty   = 0.0
        self.step_remaining = 0   # segundos restantes na etapa atual (0 = sem contagem)
        self.gun_active     = False
        self.fan_active     = False
        self.running        = False
        self.finished       = False
        self.error_msg      = ""
        self.profile_key    = PROFILE_KEYS[0]
        self.beep_queue     = []
        # Estado dos controles manuais
        self.manual_fan     = False
        self.manual_base    = False
        self.manual_gun     = False

    def push_beep(self, btype):
        self.beep_queue.append(btype)

state = SharedState()


# ═══════════════════════════════════════════════════════
# HELPER — ventilador com lógica configurável
# ═══════════════════════════════════════════════════════
def fan_write(on: bool):
    """Liga/desliga o ventilador respeitando FAN_ACTIVE_HIGH."""
    GPIO.output(FAN_PIN, GPIO.HIGH if (on == FAN_ACTIVE_HIGH) else GPIO.LOW)


def buzzer_beep(count=1, on_ms=600, off_ms=300):
    """Aciona o buzzer físico em thread separada. Ativo em LOW."""
    def _beep():
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(BUZZER_PIN, GPIO.OUT)
            for i in range(count):
                GPIO.output(BUZZER_PIN, GPIO.LOW)    # liga
                time.sleep(on_ms / 1000.0)
                GPIO.output(BUZZER_PIN, GPIO.HIGH)   # desliga
                if i < count - 1:
                    time.sleep(off_ms / 1000.0)
        except Exception:
            pass
    threading.Thread(target=_beep, daemon=True).start()


# ═══════════════════════════════════════════════════════
# MAX6675
# ═══════════════════════════════════════════════════════
class MAX6675:
    MIN_INTERVAL = 0.30

    def __init__(self, device=0):
        self.spi = spidev.SpiDev()
        self.spi.open(0, device)
        self.spi.max_speed_hz = 500_000
        self.spi.mode = 0b01
        self._last_read  = 0.0
        self._last_valid = None

    def read_celsius(self):
        wait = self.MIN_INTERVAL - (time.time() - self._last_read)
        if wait > 0:
            time.sleep(wait)
        raw   = self.spi.readbytes(2)
        self._last_read = time.time()
        value = (raw[0] << 8) | raw[1]
        if value & 0x4:
            return None
        temp = ((value >> 3) & 0x1FFF) * 0.25
        if temp == 0.0:
            return self._last_valid
        if self._last_valid is not None and self._last_valid > 50 and temp < 20:
            return self._last_valid
        self._last_valid = temp
        return temp

    def close(self):
        self.spi.close()


# ═══════════════════════════════════════════════════════
# PID
# ═══════════════════════════════════════════════════════
class PID:
    def __init__(self, kp, ki, kd, max_integral=50):
        self.kp = kp; self.ki = ki; self.kd = kd
        self.max_integral = max_integral
        self._integral = 0.0; self._prev_error = 0.0; self._last_time = None

    def reset(self):
        self._integral = 0.0; self._prev_error = 0.0; self._last_time = None

    def compute(self, setpoint, measured):
        now = time.time()
        if self._last_time is None:
            self._last_time = now; return 0.0
        dt = now - self._last_time
        if dt <= 0: return 0.0
        error          = setpoint - measured
        self._integral = max(-self.max_integral,
                             min(self.max_integral, self._integral + error * dt))
        derivative     = (error - self._prev_error) / dt
        output         = self.kp * error + self.ki * self._integral + self.kd * derivative
        self._prev_error = error; self._last_time = now
        return max(0.0, min(100.0, output))


# ═══════════════════════════════════════════════════════
# SSR
# ═══════════════════════════════════════════════════════
class SSRController:
    def __init__(self, pin, window=2.0):
        self.pin = pin; self.window = window
        self._window_start = time.time(); self._duty = 0.0
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    def set_duty(self, percent):
        self._duty = max(0.0, min(100.0, percent))

    def update(self):
        elapsed = time.time() - self._window_start
        if elapsed >= self.window:
            self._window_start = time.time(); elapsed = 0.0
        GPIO.output(self.pin,
                    GPIO.HIGH if elapsed < (self._duty / 100.0) * self.window else GPIO.LOW)

    def off(self):
        self._duty = 0.0; GPIO.output(self.pin, GPIO.LOW)


# ═══════════════════════════════════════════════════════
# LOOP DE CONTROLE
# ═══════════════════════════════════════════════════════
def control_loop():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    with state.lock:
        profile = PROFILES[state.profile_key]

    base_sensor = MAX6675(device=CS_BASE)
    gun_sensor  = MAX6675(device=CS_GUN)
    base_pid    = PID(BASE_KP, BASE_KI, BASE_KD, MAX_INTEGRAL)
    gun_pid     = PID(GUN_KP,  GUN_KI,  GUN_KD,  MAX_INTEGRAL)
    base_ssr    = SSRController(SSR_BASE_PIN, WINDOW_SIZE)
    gun_ssr     = SSRController(SSR_GUN_PIN,  WINDOW_SIZE)

    # Inicializa ventilador desligado com lógica correta
    GPIO.setup(FAN_PIN, GPIO.OUT)
    fan_write(False)

    start  = time.time()
    gun_sp = profile["gun_sp"]
    steps  = profile["steps"]

    chip_remove_beeped = False
    fan_on             = False

    log_file = os.path.join(
        LOG_DIR,
        datetime.now().strftime(f"run_%Y%m%d_%H%M%S_{state.profile_key}.csv")
    )

    with open(log_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "step", "base_sp", "base_temp", "base_duty",
                         "gun_sp", "gun_temp", "gun_duty", "fan"])

        try:
            for i, (name, base_target, duration) in enumerate(steps):
                base_pid.reset()
                gun_pid.reset()
                step_start         = time.time()
                is_reflow          = (name == "Reflow")
                is_cool            = (name == "Resfria")
                chip_remove_beeped = False
                gun_on_since       = None   # controle de tempo mínimo do canhão

                with state.lock:
                    ramp_start_temp = state.cur_base_temp or base_target

                with state.lock:
                    state.step_name = f"{name} → {base_target}°C"
                    state.step_idx  = i
                    state.push_beep("step")
                buzzer_beep(count=1, on_ms=600)  # 1 bip — troca de etapa

                while state.running:
                    elapsed = time.time() - step_start
                    t_total = time.time() - start

                    base_temp = base_sensor.read_celsius()
                    gun_temp  = gun_sensor.read_celsius()

                    # Rampa suavizada só no Pre-heat (i == 0)
                    if i == 0 and duration > 0:
                        ramped_sp = min(float(base_target),
                                        ramp_start_temp + RAMP_RATE * elapsed)
                    else:
                        ramped_sp = float(base_target)

                    # Segurança — termopar base
                    if base_temp is None:
                        base_ssr.off(); gun_ssr.off()
                        with state.lock:
                            state.error_msg = "⚠ Termopar da base desconectado!"
                        time.sleep(1); continue
                    else:
                        with state.lock:
                            state.error_msg = ""

                    # Segurança — temperatura máxima
                    if base_temp > MAX_TEMP:
                        base_ssr.off(); gun_ssr.off(); fan_write(False)
                        with state.lock:
                            state.error_msg = f"⚠ SEGURANÇA: base {base_temp:.1f}°C > {MAX_TEMP}°C!"
                            state.running = False
                        break

                    # ── Controle da base ──
                    if duration == 0:
                        base_duty = 0.0; base_ssr.set_duty(0)
                        done = base_temp <= base_target
                    else:
                        base_duty = base_pid.compute(ramped_sp, base_temp)
                        base_ssr.set_duty(base_duty)
                        done = elapsed >= duration

                    # ── Controle do canhão — bang-bang com pulso mínimo ──
                    # Pulso mínimo de GUN_MIN_ON_S garante que o ar quente
                    # tem tempo de fluir até a placa antes de desligar.
                    if is_reflow:
                        gt = gun_temp if gun_temp is not None else 0.0
                        now = time.time()
                        if gt < gun_sp:
                            # Abaixo do SP — liga e registra quando ligou
                            if gun_on_since is None:
                                gun_on_since = now
                            gun_duty = 100.0
                        else:
                            # Acima do SP — só desliga se já passou o tempo mínimo
                            if gun_on_since is not None and (now - gun_on_since) >= GUN_MIN_ON_S:
                                gun_duty = 0.0
                                gun_on_since = None
                            else:
                                gun_duty = 100.0   # mantém ligado até cumprir tempo mínimo
                        gun_ssr.set_duty(gun_duty)
                        gun_on = True
                        if base_temp >= CHIP_REMOVE_TEMP and not chip_remove_beeped:
                            chip_remove_beeped = True
                            with state.lock:
                                state.push_beep("remove_chip")
                            buzzer_beep(count=3, on_ms=700, off_ms=300)  # 3 bips longos — SACAR CHIP
                    else:
                        gun_duty = 0.0; gun_ssr.set_duty(0); gun_on = False

                    # ── Ventilador ──
                    if is_cool:
                        if base_temp <= FAN_ON_TEMP and not fan_on:
                            fan_write(True); fan_on = True
                            with state.lock:
                                state.fan_active = True
                        if fan_on and base_temp <= FAN_OFF_TEMP:
                            fan_write(False); fan_on = False
                            with state.lock:
                                state.fan_active = False
                    else:
                        if fan_on:
                            fan_write(False); fan_on = False
                            with state.lock:
                                state.fan_active = False

                    # ── Atualiza SSRs ──
                    base_ssr.update()
                    if is_reflow:
                        gun_ssr.update()
                    else:
                        gun_ssr.off()   # garante canhão desligado fora do Reflow

                    # ── Estado compartilhado ──
                    with state.lock:
                        state.times.append(round(t_total, 1))
                        state.base_temps.append(round(base_temp, 2))
                        state.gun_temps.append(round(gun_temp, 2) if gun_temp else None)
                        state.setpoints.append(round(ramped_sp, 1))
                        state.base_duties.append(round(base_duty, 1))
                        state.gun_duties.append(round(gun_duty, 1))
                        state.cur_base_temp = base_temp
                        state.cur_gun_temp  = gun_temp or 0.0
                        state.cur_base_sp   = ramped_sp
                        state.cur_base_duty = base_duty
                        state.cur_gun_duty  = gun_duty
                        state.gun_active    = gun_on
                        state.fan_active    = fan_on
                        # Contagem regressiva — só faz sentido em etapas com duração fixa
                        if duration > 0:
                            state.step_remaining = max(0, int(duration - elapsed))
                        else:
                            state.step_remaining = 0

                    writer.writerow([
                        f"{t_total:.1f}", name, f"{ramped_sp:.1f}",
                        f"{base_temp:.2f}", f"{base_duty:.1f}",
                        gun_sp if is_reflow else 0,
                        f"{gun_temp:.2f}" if gun_temp else "---",
                        f"{gun_duty:.1f}", 1 if fan_on else 0
                    ])
                    f.flush()
                    time.sleep(0.25)

                    if done:
                        if is_reflow:
                            with state.lock:
                                state.push_beep("gun_off")
                            buzzer_beep(count=2, on_ms=600, off_ms=300)  # 2 bips — reflow concluído
                        break

            with state.lock:
                state.step_name = "✓ Concluído — placa pode ser removida"
                state.finished  = True
                state.push_beep("done")
            buzzer_beep(count=1, on_ms=1200)  # 1 bip longo — ciclo concluído

        except Exception as e:
            with state.lock:
                state.error_msg = f"Erro: {e}"
        finally:
            # Garante que canhão e base estão desligados antes de liberar o GPIO
            gun_ssr.off()
            base_ssr.off()
            fan_write(False)
            time.sleep(0.3)   # aguarda beeps pendentes antes do cleanup
            base_sensor.close(); gun_sensor.close()
            GPIO.cleanup()
            with state.lock:
                state.running    = False
                state.gun_active = False
                state.fan_active = False
            print(f"Log salvo: {log_file}")


# ═══════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════
HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reballing</title>
<script src="/chart.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #141414; color: #e0e0e0;
  font-family: monospace; font-size: 12px;
  display: grid;
  grid-template-columns: 190px 1fr;
  grid-template-rows: auto 1fr;
  height: 100vh; overflow: hidden;
}
header {
  grid-column: 1 / -1;
  background: #1e1e1e; border-bottom: 1px solid #2a2a2a;
  padding: 5px 12px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
header h1 { font-size: 0.75rem; color: #888; letter-spacing: 2px; white-space: nowrap; }
.err-bar { color: #ff6b6b; font-size: 0.75rem; flex: 1; text-align: right; }
aside {
  background: #1a1a1a; border-right: 1px solid #2a2a2a;
  padding: 6px 6px; display: flex; flex-direction: column; gap: 3px;
  overflow-y: auto; height: 100%;
}
.section-label { font-size: 0.58rem; color: #555; text-transform: uppercase; letter-spacing: 1px; padding: 0 4px; margin-bottom: 2px; }
.btn-new-profile {
  font-size: 0.6rem; padding: 3px 8px; border-radius: 4px;
  border: 1px solid #4a3a7a; background: #1e1830; color: #a78bfa;
  cursor: pointer; font-family: monospace;
}
.btn-new-profile:hover { background: #2a2040; }
.pcard-actions { display: flex; gap: 4px; margin-top: 4px; }
.pcard-actions button {
  font-size: 0.55rem; padding: 2px 6px; border-radius: 3px;
  border: 1px solid #333; background: #1a1a1a; color: #666;
  cursor: pointer; font-family: monospace;
}
.pcard-actions button:hover { background: #252525; color: #aaa; }
.pcard-actions button.danger:hover { background: #2a1010; color: #ff6b6b; border-color: #5a2020; }
.group-label {
  font-size: 0.58rem; color: #6d5fc7; text-transform: uppercase;
  letter-spacing: 1px; padding: 4px 4px 2px 4px;
  margin-top: 6px; border-top: 1px solid #2a2a2a;
}
.pcard { padding: 5px 8px; border-radius: 5px; cursor: pointer; border: 1px solid transparent; transition: background .12s, border-color .12s; }
.pcard:hover { background: #242424; }
.pcard.selected { background: #1e1830; border-color: #6d5fc7; }
.pcard.locked   { opacity: 0.4; pointer-events: none; }
.pcard .pl  { font-size: 0.72rem; color: #ccc; font-weight: bold; }
.pcard .pd  { font-size: 0.58rem; color: #666; margin-top: 1px; line-height: 1.3; }
.pcard .ps  { font-size: 0.56rem; color: #444; margin-top: 3px; border-top: 1px solid #2a2a2a; padding-top: 3px; line-height: 1.6; }
.pcard.selected .pd { color: #7a6aaa; }
.pcard.selected .ps { color: #5a4a8a; border-color: #2e2550; }
main { padding: 5px 8px; display: flex; flex-direction: column; gap: 4px; overflow: hidden; height: 100%; }
.status-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
.scard { background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 5px; padding: 4px 8px; }
.scard .sl { font-size: 0.56rem; color: #555; text-transform: uppercase; letter-spacing: 1px; }
.scard .sv { font-size: 1.15rem; font-weight: bold; margin-top: 1px; line-height: 1; }
.scard.base-t .sv { color: #ff6b6b; }
.scard.gun-t  .sv { color: #f97316; }
.scard.duty   .sv { color: #6bcb77; }
.scard.step   .sv { font-size: 0.68rem; color: #a78bfa; margin-top: 2px; line-height: 1.3; }
.scard.timer  .sv { color: #ffd93d; }
.indicator-row { display: flex; gap: 5px; align-items: center; }
.ind { padding: 2px 8px; border-radius: 3px; font-size: 0.62rem; border: 1px solid #2a2a2a; color: #555; background: #1a1a1a; transition: all .2s; }
.ind.on-gun { border-color: #f97316; color: #f97316; background: #1f1208; }
.ind.on-fan { border-color: #38bdf8; color: #38bdf8; background: #071620; }
.pills { display: flex; gap: 4px; flex-wrap: wrap; }
.pill { padding: 2px 7px; border-radius: 20px; font-size: 0.6rem; background: #1e1e1e; border: 1px solid #333; color: #666; }
.pill.active { background: #2a1f52; border-color: #7c6fd4; color: #a78bfa; }
.pill.done   { background: #122418; border-color: #3a6644; color: #6bcb77; }
.chart-box { background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 5px; padding: 4px 8px; }
.btn-row { display: flex; gap: 6px; align-items: center; }
.btn { padding: 5px 14px; border-radius: 4px; font-family: monospace; font-size: 0.75rem; cursor: pointer; border: 1px solid #444; background: #252525; color: #ccc; }
.btn:hover { background: #333; }
.btn.start    { border-color: #4a9a5a; color: #6bcb77; }
.btn.stop     { border-color: #8a3a3a; color: #ff6b6b; }
.btn.shutdown { border-color: #6a2a2a; color: #cc4444; margin-left: auto; }
.btn.shutdown:hover { background: #2a1010; }
.btn:disabled { opacity: 0.35; cursor: default; }
.manual-section {
  background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 5px;
  padding: 4px 10px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.manual-label { font-size: 0.58rem; color: #555; text-transform: uppercase; letter-spacing: 1px; white-space: nowrap; }
.btn-tog {
  padding: 3px 10px; border-radius: 4px; font-family: monospace; font-size: 0.7rem;
  cursor: pointer; border: 1px solid #383838; background: #222; color: #666; transition: all .15s;
}
.btn-tog:hover:not(:disabled) { background: #2e2e2e; color: #aaa; }
.btn-tog.active-fan  { border-color: #38bdf8; color: #38bdf8; background: #071620; }
.btn-tog.active-base { border-color: #ff6b6b; color: #ff6b6b; background: #1a0808; }
.btn-tog.active-gun  { border-color: #f97316; color: #f97316; background: #1a0d05; }
.btn-tog:disabled    { opacity: 0.3; cursor: default; }
#toast {
  position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%) translateY(60px);
  background: #1e1e1e; border: 1px solid #555; border-radius: 6px;
  padding: 8px 20px; font-size: 0.82rem; color: #fff;
  transition: transform .3s ease; z-index: 100; white-space: nowrap; pointer-events: none;
}
#toast.show    { transform: translateX(-50%) translateY(0); }
#toast.alert   { border-color: #ff6b6b; color: #ff6b6b; background: #1a0a0a; }
#toast.success { border-color: #6bcb77; color: #6bcb77; background: #0a1a0a; }
#toast.warn    { border-color: #f97316; color: #f97316; background: #1a0d05; }

#disconnect-banner {
  display: none;
  position: fixed;
  top: 0; left: 0; right: 0;
  background: #2a0a0a;
  border-bottom: 2px solid #ff6b6b;
  color: #ff6b6b;
  padding: 10px 16px;
  font-family: monospace;
  font-size: 0.85rem;
  align-items: center;
  justify-content: center;
  gap: 16px;
  z-index: 200;
}
#disconnect-banner button {
  padding: 5px 16px;
  border-radius: 4px;
  font-family: monospace;
  font-size: 0.8rem;
  cursor: pointer;
  border: 1px solid #ff6b6b;
  background: #3a1010;
  color: #ff6b6b;
}
#disconnect-banner button:hover {
  background: #4a1515;
}

/* ── Modal de Criar/Editar Perfil ── */
#profile-modal-overlay {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  z-index: 300;
  align-items: center; justify-content: center;
}
#profile-modal-overlay.show { display: flex; }
#profile-modal {
  background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
  width: 520px; max-width: 92vw; max-height: 88vh;
  display: flex; flex-direction: column;
  font-family: monospace;
}
.pm-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid #2a2a2a;
  font-size: 0.95rem; color: #a78bfa;
}
.pm-header button {
  background: none; border: none; color: #666; cursor: pointer; font-size: 1rem;
}
.pm-header button:hover { color: #ccc; }
.pm-body { padding: 14px 18px; overflow-y: auto; flex: 1; }
.pm-row { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.pm-row label { font-size: 0.65rem; color: #777; text-transform: uppercase; letter-spacing: 0.5px; }
.pm-row input {
  background: #111; border: 1px solid #333; border-radius: 4px;
  padding: 7px 10px; color: #ddd; font-family: monospace; font-size: 0.8rem;
}
.pm-row input:focus { outline: none; border-color: #6d5fc7; }
.pm-row-split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pm-steps-header {
  display: flex; justify-content: space-between; align-items: center;
  margin: 16px 0 8px 0; padding-top: 10px; border-top: 1px solid #2a2a2a;
}
.pm-steps-header span { font-size: 0.7rem; color: #777; text-transform: uppercase; letter-spacing: 0.5px; }
.pm-steps-header button {
  font-size: 0.65rem; padding: 4px 10px; border-radius: 4px;
  border: 1px solid #4a3a7a; background: #1e1830; color: #a78bfa; cursor: pointer;
  font-family: monospace;
}
.pm-steps-header button:hover { background: #2a2040; }
.pm-step-row {
  display: grid; grid-template-columns: 1fr 70px 70px 28px; gap: 6px;
  align-items: center; margin-bottom: 6px;
}
.pm-step-row input {
  background: #111; border: 1px solid #333; border-radius: 4px;
  padding: 6px 8px; color: #ddd; font-family: monospace; font-size: 0.75rem;
}
.pm-step-row input:focus { outline: none; border-color: #6d5fc7; }
.pm-step-remove {
  background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
  color: #666; cursor: pointer; font-size: 0.7rem; padding: 6px;
}
.pm-step-remove:hover { background: #2a1010; color: #ff6b6b; border-color: #5a2020; }
.pm-error {
  color: #ff6b6b; font-size: 0.72rem; margin-top: 8px; min-height: 1em;
}
.pm-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 12px 18px; border-top: 1px solid #2a2a2a;
}
.pm-footer button {
  padding: 7px 18px; border-radius: 5px; font-family: monospace; font-size: 0.78rem;
  cursor: pointer; border: 1px solid #444; background: #252525; color: #ccc;
}
#pm-save { border-color: #4a9a5a; color: #6bcb77; }
#pm-save:hover { background: #1a2a1a; }
#pm-cancel:hover { background: #2a2a2a; }
</style>
</head>
<body>

<header>
  <h1>⚡ REBALLING — CONTROLADOR</h1>
  <span class="err-bar" id="err"></span>
</header>

<aside>
  <div class="section-label" style="display:flex; justify-content:space-between; align-items:center">
    <span>Perfil de temperatura</span>
    <button id="btn-new-profile" class="btn-new-profile" title="Criar novo perfil">+ Novo</button>
  </div>
  <div id="profiles-list"></div>
</aside>

<main>

  <div class="status-row">
    <div class="scard base-t"><div class="sl">Base</div><div class="sv" id="cur-base">--°C</div></div>
    <div class="scard gun-t" ><div class="sl">Canhão</div><div class="sv" id="cur-gun">--°C</div></div>
    <div class="scard duty"  ><div class="sl">Duty base</div><div class="sv" id="cur-duty-base">--%</div></div>
    <div class="scard duty"  ><div class="sl">Duty canhão</div><div class="sv" id="cur-duty-gun">--%</div></div>
    <div class="scard step"  ><div class="sl">Etapa</div><div class="sv" id="cur-step">—</div></div>
    <div class="scard timer" ><div class="sl">Restante</div><div class="sv" id="cur-timer">--:--</div></div>
    <div class="scard timer" ><div class="sl">Total</div><div class="sv" id="cur-total">--:--</div></div>
  </div>

  <div class="indicator-row">
    <div class="ind" id="ind-gun">🔥 Canhão</div>
    <div class="ind" id="ind-fan">💨 Ventilador</div>
    <div class="pills" id="steps-bar" style="margin-left:8px"></div>
  </div>

  <div class="chart-box" style="height:160px; flex-shrink:0">
    <canvas id="chartTemp" height="148"></canvas>
  </div>

  <div class="chart-box" style="height:55px; flex-shrink:0">
    <canvas id="chartDuty" height="44"></canvas>
  </div>

  <div class="btn-row">
    <button class="btn start"    id="btn-start">▶ Iniciar</button>
    <button class="btn stop"     id="btn-stop"  disabled>■ Parar</button>
    <button class="btn shutdown" id="btn-shutdown">⏻ Desligar</button>
  </div>

  <div class="manual-section">
    <span class="manual-label">Teste manual</span>
    <button class="btn-tog" id="mt-fan"  data-device="fan">Ventilador</button>
    <button class="btn-tog" id="mt-base" data-device="base">SSR Base</button>
    <button class="btn-tog" id="mt-gun"  data-device="gun">SSR Canhao</button>
    <span style="font-size:0.6rem;color:#3a3a3a">disponível apenas com ciclo parado</span>
  </div>

</main>

<div id="toast"></div>

<div id="disconnect-banner">
  <span>⚠ Conexão perdida com o servidor</span>
  <button id="btn-reconnect">Reconectar</button>
</div>

<div id="profile-modal-overlay">
  <div id="profile-modal">
    <div class="pm-header">
      <span id="pm-title">Novo Perfil</span>
      <button id="pm-close">✕</button>
    </div>
    <div class="pm-body">
      <div class="pm-row">
        <label>Nome do perfil</label>
        <input type="text" id="pm-label" placeholder="Ex: PS4 — APU" maxlength="40">
      </div>
      <div class="pm-row">
        <label>Descrição</label>
        <input type="text" id="pm-desc" placeholder="Ex: Chip APU do PS4, substrate fino" maxlength="80">
      </div>
      <div class="pm-row-split">
        <div class="pm-row">
          <label>Grupo</label>
          <input type="text" id="pm-group" placeholder="Ex: PS4" maxlength="20">
        </div>
        <div class="pm-row">
          <label>Setpoint do canhão (°C)</label>
          <input type="number" id="pm-gunsp" placeholder="210" min="0" max="320">
        </div>
      </div>

      <div class="pm-steps-header">
        <span>Etapas</span>
        <button id="pm-add-step" type="button">+ Adicionar etapa</button>
      </div>
      <div id="pm-steps-list"></div>

      <div class="pm-error" id="pm-error"></div>
    </div>
    <div class="pm-footer">
      <button id="pm-cancel">Cancelar</button>
      <button id="pm-save">Salvar Perfil</button>
    </div>
  </div>
</div>

{% raw %}
<script>
document.addEventListener('DOMContentLoaded', function() {

  const MAX_PTS = 600;
  let PROFILES = {};
  let selectedKey = '';
  const manualOn = { fan: false, base: false, gun: false };

  // Audio
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  let actx = null;
  function ensureAudio() {
    if (!actx) actx = new AudioCtx();
    if (actx.state === 'suspended') actx.resume();
  }
  function beep(freq, dur, vol) {
    vol = vol || 0.3;
    ensureAudio();
    const osc = actx.createOscillator();
    const gain = actx.createGain();
    osc.connect(gain); gain.connect(actx.destination);
    osc.type = 'sine'; osc.frequency.value = freq;
    gain.gain.setValueAtTime(vol, actx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, actx.currentTime + dur);
    osc.start(); osc.stop(actx.currentTime + dur);
  }
  function beepStep()       { beep(880, 0.15); }
  function beepGunOn()      { setTimeout(function(){beep(660,0.2);},0); setTimeout(function(){beep(880,0.2);},250); }
  function beepRemoveChip() { [0,400,800].forEach(function(d){setTimeout(function(){beep(1100,0.4,0.5);},d);}); }
  function beepDone()       { beep(660, 0.8, 0.3); }

  // Toast
  var toastTimer = null;
  function showToast(msg, type, duration) {
    type = type || ''; duration = duration || 4000;
    var t = document.getElementById('toast');
    t.textContent = msg; t.className = 'show ' + type;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ t.className = ''; }, duration);
  }

  // Graficos
  var chartOpts = function(ymin, ymax, ytitle) { return {
    animation: false, responsive: true, maintainAspectRatio: false,
    scales: {
      x: { ticks: { color: '#555', maxTicksLimit: 10, font:{size:10} }, grid: { color: '#222' } },
      y: { min: ymin, max: ymax, ticks: { color: '#555', font:{size:10} }, grid: { color: '#222' },
           title: { display: true, text: ytitle, color: '#444', font:{size:10} } }
    },
    plugins: { legend: { labels: { color: '#666', boxWidth: 10, font:{size:10} } } }
  }; };

  var tempChart = new Chart(document.getElementById('chartTemp'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Base C',   data: [], borderColor: '#ff6b6b', borderWidth: 2, pointRadius: 0, tension: 0.3, fill: false },
      { label: 'Canhao C', data: [], borderColor: '#f97316', borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false },
      { label: 'Setpoint', data: [], borderColor: '#ffd93d', borderWidth: 1.5, borderDash: [5,3], pointRadius: 0, tension: 0, fill: false },
    ]},
    options: chartOpts(0, 250, 'C')
  });

  var dutyChart = new Chart(document.getElementById('chartDuty'), {
    type: 'line',
    data: { labels: [], datasets: [
      { label: 'Duty base %',   data: [], borderColor: '#ff6b6b', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false },
      { label: 'Duty canhao %', data: [], borderColor: '#f97316', borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false },
    ]},
    options: chartOpts(0, 105, '%')
  });

  function clearCharts() {
    [tempChart, dutyChart].forEach(function(c) {
      c.data.labels = [];
      c.data.datasets.forEach(function(ds){ ds.data = []; });
      c.update('none');
    });
  }

  function pushData(t, baseT, gunT, sp, baseDuty, gunDuty) {
    var lbl = t + 's';
    [tempChart, dutyChart].forEach(function(c) {
      c.data.labels.push(lbl);
      if (c.data.labels.length > MAX_PTS) c.data.labels.shift();
    });
    function push(chart, idx, val) {
      chart.data.datasets[idx].data.push(val);
      if (chart.data.datasets[idx].data.length > MAX_PTS) chart.data.datasets[idx].data.shift();
    }
    push(tempChart, 0, baseT); push(tempChart, 1, gunT || null); push(tempChart, 2, sp);
    push(dutyChart, 0, baseDuty); push(dutyChart, 1, gunDuty);
    tempChart.update('none'); dutyChart.update('none');
  }

  // Pills
  function buildPills(key) {
    var bar = document.getElementById('steps-bar');
    bar.innerHTML = '';
    var profile = PROFILES[key] || {steps:[]};
    profile.steps.forEach(function(step, i) {
      var d = document.createElement('div');
      d.className = 'pill'; d.id = 'pill-' + i; d.textContent = step[0];
      bar.appendChild(d);
    });
  }

  // Renderiza a lista de perfis na sidebar (agrupados, com acoes para customizados)
  function renderProfilesList() {
    var container = document.getElementById('profiles-list');
    container.innerHTML = '';
    var lastGroup = null;
    var first = true;

    Object.keys(PROFILES).forEach(function(key) {
      var p = PROFILES[key];

      if (p.group !== lastGroup) {
        var gl = document.createElement('div');
        gl.className = 'group-label';
        gl.textContent = p.group;
        container.appendChild(gl);
        lastGroup = p.group;
      }

      var card = document.createElement('div');
      card.className = 'pcard' + (first ? ' selected' : '');
      card.id = 'pc-' + key;
      card.dataset.key = key;
      if (first) { selectedKey = key; first = false; }

      var stepsHtml = p.steps.map(function(s) {
        var name = s[0], sp = s[1], dur = s[2];
        return name + ': ' + sp + 'C' + (dur > 0 ? ' / ' + dur + 's' : ' v') + '<br>';
      }).join('');

      card.innerHTML =
        '<div class="pl">' + p.label + '</div>' +
        '<div class="pd">' + (p.desc || '') + '</div>' +
        '<div class="ps">' + stepsHtml + 'Canhao: ' + p.gun_sp + 'C</div>';

      if (p.custom) {
        var actions = document.createElement('div');
        actions.className = 'pcard-actions';
        actions.innerHTML =
          '<button class="pm-edit-btn">Editar</button>' +
          '<button class="pm-delete-btn danger">Excluir</button>';
        card.appendChild(actions);

        actions.querySelector('.pm-edit-btn').addEventListener('click', function(e) {
          e.stopPropagation();
          openProfileModal(key);
        });
        actions.querySelector('.pm-delete-btn').addEventListener('click', function(e) {
          e.stopPropagation();
          if (confirm('Excluir o perfil "' + p.label + '"? Essa acao nao pode ser desfeita.')) {
            fetch('/profiles/' + key, { method: 'DELETE' }).then(function(r) {
              if (r.ok) {
                fetch('/profiles').then(function(r2){ return r2.json(); }).then(function(data) {
                  PROFILES = data;
                  renderProfilesList();
                });
              } else {
                r.text().then(function(msg){ showToast(msg, 'alert', 4000); });
              }
            });
          }
        });
      }

      card.addEventListener('click', function() { selectProfile(this.dataset.key); });
      container.appendChild(card);
    });
  }

  // Selecao de perfil
  function selectProfile(key) {
    if (document.getElementById('btn-start').disabled) return;
    selectedKey = key;
    document.querySelectorAll('.pcard').forEach(function(c){
      c.classList.toggle('selected', c.dataset.key === key);
    });
    buildPills(key);
    clearCharts();
  }

  // Botao Iniciar
  document.getElementById('btn-start').addEventListener('click', function() {
    ensureAudio();
    clearCharts();
    document.querySelectorAll('.pill').forEach(function(p){ p.className = 'pill'; });
    fetch('/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ profile: selectedKey })
    });
  });

  // Botao Parar
  document.getElementById('btn-stop').addEventListener('click', function() {
    fetch('/stop', { method: 'POST' });
  });

  // Botao Desligar
  document.getElementById('btn-shutdown').addEventListener('click', function() {
    if (!confirm('Desligar o Raspberry Pi agora?')) return;
    fetch('/shutdown', { method: 'POST' });
    showToast('Desligando... aguarde.', 'warn', 10000);
    this.disabled = true;
  });

  // Controle manual
  function updateManualBtn(device) {
    var btn = document.getElementById('mt-' + device);
    if (!btn) return;
    var on = manualOn[device];
    var cls = { fan: 'active-fan', base: 'active-base', gun: 'active-gun' };
    btn.className = 'btn-tog' + (on ? ' ' + cls[device] : '');
  }

  document.querySelectorAll('.btn-tog').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var device = this.dataset.device;
      var newState = !manualOn[device];
      fetch('/manual', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ device: device, on: newState })
      }).then(function(r) {
        if (r.ok) {
          manualOn[device] = newState;
          updateManualBtn(device);
        } else {
          showToast('Ciclo em andamento - pare antes de usar o controle manual', 'warn');
        }
      });
    });
  });

  // SSE — sem retry automático. Em caso de erro, mostra banner
  // persistente com botão "Reconectar" — só tenta de novo quando
  // o usuário clicar, evitando loop de reconexão infinito.
  var currentES = null;

  function showDisconnectedBanner() {
    var b = document.getElementById('disconnect-banner');
    b.style.display = 'flex';
  }

  function hideDisconnectedBanner() {
    var b = document.getElementById('disconnect-banner');
    b.style.display = 'none';
  }

  function connectSSE() {
    if (currentES) {
      currentES.close();
      currentES = null;
    }

    var es = new EventSource('/stream');
    currentES = es;

    es.onopen = function() {
      hideDisconnectedBanner();
    };

    es.onmessage = function(e) {
      var d = JSON.parse(e.data);

      document.getElementById('cur-base').textContent      = d.base_temp.toFixed(1) + 'C';
      document.getElementById('cur-gun').textContent       = d.gun_temp > 0 ? d.gun_temp.toFixed(1) + 'C' : '--C';
      document.getElementById('cur-duty-base').textContent = d.base_duty.toFixed(1) + '%';
      document.getElementById('cur-duty-gun').textContent  = d.gun_active ? d.gun_duty.toFixed(1) + '%' : '--';
      document.getElementById('cur-step').textContent      = d.step;

      // Contagem regressiva da etapa
      var rem = d.step_remaining || 0;
      if (rem > 0 && d.running) {
        var mm = Math.floor(rem / 60);
        var ss = rem % 60;
        document.getElementById('cur-timer').textContent = mm + ':' + (ss < 10 ? '0' : '') + ss;
      } else {
        document.getElementById('cur-timer').textContent = '--:--';
      }

      // Tempo total do processo
      if (d.running || d.t > 0) {
        var totS = Math.floor(d.t || 0);
        var tmm = Math.floor(totS / 60);
        var tss = totS % 60;
        document.getElementById('cur-total').textContent = tmm + ':' + (tss < 10 ? '0' : '') + tss;
      } else {
        document.getElementById('cur-total').textContent = '--:--';
      }
      document.getElementById('err').textContent           = d.error || '';

      document.querySelectorAll('.pill').forEach(function(p, i) {
        p.className = 'pill' + (i === d.step_idx ? ' active' : i < d.step_idx ? ' done' : '');
      });

      document.getElementById('ind-gun').className = 'ind' + (d.gun_active ? ' on-gun' : '');
      document.getElementById('ind-fan').className = 'ind' + (d.fan_active ? ' on-fan' : '');

      var run = d.running;
      document.getElementById('btn-start').disabled = run;
      document.getElementById('btn-stop').disabled  = !run;
      document.querySelectorAll('.pcard').forEach(function(c){ c.classList.toggle('locked', run); });
      document.querySelectorAll('.btn-tog').forEach(function(b){ b.disabled = run; });
      if (run) {
        Object.keys(manualOn).forEach(function(k){ manualOn[k] = false; updateManualBtn(k); });
      }

      if (d.beeps && d.beeps.length > 0) {
        d.beeps.forEach(function(b) {
          if (b === 'step')         { beepStep();       showToast('Etapa: ' + d.step, '', 3000); }
          else if (b === 'gun_on')  { beepGunOn();      showToast('Canhao ativo!', 'warn', 4000); }
          else if (b === 'gun_off') { beepDone();       showToast('Reflow concluido — canhao desligando', 'warn', 5000); }
          else if (b === 'remove_chip') { beepRemoveChip(); showToast('SACAR O CHIP AGORA!', 'alert', 8000); }
          else if (b === 'done')    { beepDone();       showToast('Placa pode ser removida', 'success', 10000); }
        });
      }

      if (run) pushData(d.t, d.base_temp, d.gun_temp, d.base_sp, d.base_duty, d.gun_duty);
    };

    es.onerror = function() {
      es.close();
      if (currentES === es) currentES = null;
      showDisconnectedBanner();
      // Sem retry automático — aguarda o usuário clicar em "Reconectar"
    };
  }

  document.getElementById('btn-reconnect').addEventListener('click', function() {
    hideDisconnectedBanner();
    connectSSE();
  });

  // Init
  fetch('/profiles').then(function(r){ return r.json(); }).then(function(data) {
    PROFILES = data;
    renderProfilesList();
    buildPills(selectedKey);
  });

  connectSSE();

  // ════════════════════════════════════════════════
  // MODAL — Criar / Editar Perfil
  // ════════════════════════════════════════════════
  var editingKey = null;   // null = criando novo; senão, editando essa key
  var stepCounter = 0;

  function stepRowHtml(id, name, sp, dur) {
    return '<div class="pm-step-row" data-step-id="' + id + '">' +
      '<input type="text" class="pm-step-name" placeholder="Nome (ex: Reflow)" value="' + (name||'') + '" maxlength="12">' +
      '<input type="number" class="pm-step-sp" placeholder="SP C" value="' + (sp!=null?sp:'') + '" min="0" max="320">' +
      '<input type="number" class="pm-step-dur" placeholder="Seg" value="' + (dur!=null?dur:'') + '" min="0" max="999">' +
      '<button type="button" class="pm-step-remove" title="Remover etapa">x</button>' +
      '</div>';
  }

  function addStepRow(name, sp, dur) {
    stepCounter++;
    var list = document.getElementById('pm-steps-list');
    var div = document.createElement('div');
    div.innerHTML = stepRowHtml(stepCounter, name, sp, dur);
    var row = div.firstChild;
    list.appendChild(row);
    row.querySelector('.pm-step-remove').addEventListener('click', function() {
      row.remove();
    });
  }

  function clearStepRows() {
    document.getElementById('pm-steps-list').innerHTML = '';
  }

  function openProfileModal(key) {
    editingKey = key || null;
    document.getElementById('pm-error').textContent = '';
    clearStepRows();

    if (editingKey && PROFILES[editingKey]) {
      var p = PROFILES[editingKey];
      document.getElementById('pm-title').textContent = 'Editar Perfil';
      document.getElementById('pm-label').value = p.label || '';
      document.getElementById('pm-desc').value  = p.desc  || '';
      document.getElementById('pm-group').value = p.group || '';
      document.getElementById('pm-gunsp').value = p.gun_sp || '';
      p.steps.forEach(function(s) { addStepRow(s[0], s[1], s[2]); });
    } else {
      document.getElementById('pm-title').textContent = 'Novo Perfil';
      document.getElementById('pm-label').value = '';
      document.getElementById('pm-desc').value  = '';
      document.getElementById('pm-group').value = '';
      document.getElementById('pm-gunsp').value = '';
      // Etapas padrão para começar — usuário pode editar/remover/adicionar
      addStepRow('Pre-heat', 80, 120);
      addStepRow('Aquec.',  150, 180);
      addStepRow('Soak',    150,  60);
      addStepRow('Reflow',  220, 120);
      addStepRow('Resfria',  50,   0);
    }

    document.getElementById('profile-modal-overlay').classList.add('show');
  }

  function closeProfileModal() {
    document.getElementById('profile-modal-overlay').classList.remove('show');
    editingKey = null;
  }

  document.getElementById('btn-new-profile').addEventListener('click', function() {
    openProfileModal(null);
  });
  document.getElementById('pm-close').addEventListener('click', closeProfileModal);
  document.getElementById('pm-cancel').addEventListener('click', closeProfileModal);
  document.getElementById('profile-modal-overlay').addEventListener('click', function(e) {
    if (e.target === this) closeProfileModal();
  });
  document.getElementById('pm-add-step').addEventListener('click', function() {
    addStepRow('', '', '');
  });

  function slugify(text) {
    return text.toLowerCase()
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '')   // remove acentos
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .substring(0, 30);
  }

  document.getElementById('pm-save').addEventListener('click', function() {
    var label = document.getElementById('pm-label').value.trim();
    var desc  = document.getElementById('pm-desc').value.trim();
    var group = document.getElementById('pm-group').value.trim();
    var gunSp = parseInt(document.getElementById('pm-gunsp').value, 10) || 0;
    var errEl = document.getElementById('pm-error');

    if (!label) { errEl.textContent = 'Informe o nome do perfil.'; return; }

    var rows = document.querySelectorAll('#pm-steps-list .pm-step-row');
    if (rows.length === 0) { errEl.textContent = 'Adicione pelo menos uma etapa.'; return; }

    var steps = [];
    for (var i = 0; i < rows.length; i++) {
      var name = rows[i].querySelector('.pm-step-name').value.trim();
      var sp   = rows[i].querySelector('.pm-step-sp').value;
      var dur  = rows[i].querySelector('.pm-step-dur').value;
      if (!name || sp === '') {
        errEl.textContent = 'Preencha nome e setpoint de todas as etapas.';
        return;
      }
      steps.push({ name: name, sp: parseInt(sp, 10), dur: parseInt(dur || '0', 10) });
    }

    errEl.textContent = '';
    var payload = { label: label, desc: desc, group: group, gun_sp: gunSp, steps: steps };

    var req;
    if (editingKey) {
      req = fetch('/profiles/' + editingKey, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
    } else {
      var key = slugify(label);
      if (!key) { errEl.textContent = 'Nome invalido para gerar identificador.'; return; }
      payload.key = key;
      req = fetch('/profiles', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
    }

    req.then(function(r) {
      if (r.ok) {
        closeProfileModal();
        fetch('/profiles').then(function(r2){ return r2.json(); }).then(function(data) {
          PROFILES = data;
          renderProfilesList();
        });
        showToast('Perfil salvo com sucesso', 'success', 3000);
      } else {
        r.text().then(function(msg) { errEl.textContent = msg; });
      }
    });
  });

});
</script>
{% endraw %}
</body>
</html>
"""


# ═══════════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════════
app = Flask(__name__)
ctrl_thread = None


# ═══════════════════════════════════════════════════════
# STANDBY — leitura contínua dos sensores quando parado
# ═══════════════════════════════════════════════════════
def standby_loop():
    base_sensor = None
    gun_sensor  = None

    # Garante que o GPIO está inicializado antes de qualquer coisa
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(FAN_PIN,      GPIO.OUT)
    GPIO.setup(SSR_BASE_PIN, GPIO.OUT)
    GPIO.setup(SSR_GUN_PIN,  GPIO.OUT)
    GPIO.setup(BUZZER_PIN,   GPIO.OUT, initial=GPIO.HIGH)  # ativo em LOW — inicia desligado
    fan_write(False)

    while True:
        with state.lock:
            running = state.running

        if running:
            if base_sensor is not None:
                try: base_sensor.close(); gun_sensor.close()
                except Exception: pass
                base_sensor = None; gun_sensor = None
            time.sleep(0.5)
            continue

        if base_sensor is None:
            try:
                base_sensor = MAX6675(device=CS_BASE)
                gun_sensor  = MAX6675(device=CS_GUN)
                print("Sensores abertos no standby.")
            except Exception as e:
                print(f"Erro ao abrir sensores no standby: {e}")
                time.sleep(1); continue

        try:
            base_temp = base_sensor.read_celsius()
            gun_temp  = gun_sensor.read_celsius()
            with state.lock:
                if base_temp is not None:
                    state.cur_base_temp = base_temp
                    if state.error_msg == "⚠ Termopar da base desconectado!":
                        state.error_msg = ""
                else:
                    state.error_msg = "⚠ Termopar da base desconectado!"
                if gun_temp is not None:
                    state.cur_gun_temp = gun_temp
        except Exception as e:
            print(f"Erro de leitura no standby: {e}")
            try: base_sensor.close(); gun_sensor.close()
            except Exception: pass
            base_sensor = None; gun_sensor = None

        time.sleep(0.6)


@app.route("/chart.js")
def serve_chartjs():
    """Serve o Chart.js localmente — sem depender de CDN externo."""
    chartjs_path = os.path.join(SCRIPT_DIR, "chart.umd.min.js")
    if not os.path.exists(chartjs_path):
        return "Chart.js não encontrado. Rode: wget https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js -O ~/reballing/chart.umd.min.js", 404
    with open(chartjs_path, "rb") as f:
        content = f.read()
    return content, 200, {"Content-Type": "application/javascript", "Cache-Control": "public, max-age=86400"}


@app.route("/")
def index():
    return render_template_string(HTML, profiles=PROFILES)


@app.route("/profiles")
def get_profiles():
    data = {
        k: {"label": v["label"], "desc": v["desc"], "group": v["group"],
            "gun_sp": v["gun_sp"],
            "steps": [[s[0], s[1], s[2]] for s in v["steps"]],
            "custom": v.get("custom", False)}
        for k, v in PROFILES.items()
    }
    return json.dumps(data), 200, {"Content-Type": "application/json"}


@app.route("/profiles", methods=["POST"])
def create_profile():
    """Cria um novo perfil customizado."""
    global PROFILE_KEYS
    data = request.get_json(silent=True) or {}

    key   = (data.get("key") or "").strip()
    label = (data.get("label") or "").strip()
    steps = data.get("steps") or []

    if not key or not label:
        return ("Chave e nome são obrigatórios", 400)
    if key in PROFILES:
        return ("Já existe um perfil com essa chave", 409)
    if not steps or len(steps) < 1:
        return ("Pelo menos uma etapa é necessária", 400)

    try:
        steps_tuples = [(s["name"], int(s["sp"]), int(s["dur"])) for s in steps]
        save_custom_profile(
            key, label,
            data.get("desc", ""),
            data.get("group", "Customizado"),
            data.get("gun_sp", 0),
            steps_tuples,
        )
    except (KeyError, ValueError, TypeError) as e:
        return (f"Dados inválidos: {e}", 400)

    PROFILE_KEYS = list(PROFILES.keys())
    return ("ok", 201)


@app.route("/profiles/<key>", methods=["PUT"])
def update_profile(key):
    """Edita um perfil customizado existente."""
    global PROFILE_KEYS
    if key in BUILTIN_PROFILE_KEYS:
        return ("Não é possível editar um perfil embutido", 403)
    if key not in PROFILES:
        return ("Perfil não encontrado", 404)

    data  = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    steps = data.get("steps") or []

    if not label:
        return ("Nome é obrigatório", 400)
    if not steps:
        return ("Pelo menos uma etapa é necessária", 400)

    try:
        steps_tuples = [(s["name"], int(s["sp"]), int(s["dur"])) for s in steps]
        save_custom_profile(
            key, label,
            data.get("desc", ""),
            data.get("group", "Customizado"),
            data.get("gun_sp", 0),
            steps_tuples,
        )
    except (KeyError, ValueError, TypeError) as e:
        return (f"Dados inválidos: {e}", 400)

    PROFILE_KEYS = list(PROFILES.keys())
    return ("ok", 200)


@app.route("/profiles/<key>", methods=["DELETE"])
def remove_profile(key):
    """Exclui um perfil customizado."""
    global PROFILE_KEYS
    if key in BUILTIN_PROFILE_KEYS:
        return ("Não é possível excluir um perfil embutido", 403)

    with state.lock:
        if state.running and state.profile_key == key:
            return ("Não é possível excluir o perfil em uso", 409)

    try:
        delete_custom_profile(key)
    except ValueError as e:
        return (str(e), 404)

    PROFILE_KEYS = list(PROFILES.keys())
    return ("ok", 200)


@app.route("/display_data")
def display_data():
    """Endpoint leve para o ESP32 — só os dados essenciais, sem histórico."""
    with state.lock:
        profile = PROFILES.get(state.profile_key, {})
        steps = [
            {"name": s[0], "sp": s[1], "dur": s[2]}
            for s in profile.get("steps", [])
        ]
        total_s = int(state.times[-1]) if state.times else 0

        data = {
            "running":        state.running,
            "base_temp":      round(state.cur_base_temp, 1),
            "gun_temp":       round(state.cur_gun_temp, 1),
            "base_duty":      round(state.cur_base_duty, 1),
            "gun_duty":       round(state.cur_gun_duty, 1),
            "gun_active":     state.gun_active,
            "fan_active":     state.fan_active,
            "step":           state.step_name,
            "step_idx":       state.step_idx,
            "step_remaining": state.step_remaining,
            "total_s":        total_s,
            "error":          state.error_msg,
            "profile_label":  profile.get("label", ""),
            "profile_key":    state.profile_key,
            "steps":          steps,
        }
    return json.dumps(data), 200, {"Content-Type": "application/json"}


@app.route("/display_profiles")
def display_profiles():
    """Lista enxuta de perfis para o menu do ESP32 — só o essencial.
    Usa a mesma ordem do dicionário PROFILES (já agrupado por plataforma)."""
    data = [
        {
            "key":    k,
            "label":  v["label"],
            "group":  v["group"],
            "gun_sp": v["gun_sp"],
            "steps":  [{"name": s[0], "sp": s[1], "dur": s[2]} for s in v["steps"]],
        }
        for k, v in PROFILES.items()
    ]
    return json.dumps(data), 200, {"Content-Type": "application/json"}


@app.route("/stream")
def stream():
    def event_gen():
        while True:
            with state.lock:
                t     = state.times[-1] if state.times else 0
                beeps = list(state.beep_queue)
                state.beep_queue.clear()
                payload = json.dumps({
                    "t": t, "base_temp": state.cur_base_temp,
                    "gun_temp": state.cur_gun_temp, "base_sp": state.cur_base_sp,
                    "base_duty": state.cur_base_duty, "gun_duty": state.cur_gun_duty,
                    "gun_active": state.gun_active, "fan_active": state.fan_active,
                    "step": state.step_name, "step_idx": state.step_idx,
                    "step_remaining": state.step_remaining,
                    "running": state.running, "error": state.error_msg, "beeps": beeps,
                })
            yield f"data: {payload}\n\n"
            time.sleep(0.5)
    return Response(event_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/start", methods=["POST"])
def start():
    global ctrl_thread
    data        = request.get_json(silent=True) or {}
    profile_key = data.get("profile", PROFILE_KEYS[0])
    if profile_key not in PROFILES:
        profile_key = PROFILE_KEYS[0]
    with state.lock:
        if state.running:
            return ("already running", 200)
        state.times = []; state.base_temps = []; state.gun_temps = []
        state.setpoints = []; state.base_duties = []; state.gun_duties = []
        state.step_name = "Iniciando..."; state.step_idx = 0
        state.finished = False; state.error_msg = ""
        state.gun_active = False; state.fan_active = False
        state.beep_queue = []; state.running = True
        state.profile_key = profile_key
    ctrl_thread = threading.Thread(target=control_loop, daemon=True)
    ctrl_thread.start()
    return ("ok", 200)


@app.route("/stop", methods=["POST"])
def stop():
    with state.lock:
        state.running = False
    return ("ok", 200)


@app.route("/manual", methods=["POST"])
def manual():
    """Aciona saídas manualmente — só fora do ciclo."""
    data   = request.get_json(silent=True) or {}
    device = data.get("device")
    on     = bool(data.get("on", False))

    with state.lock:
        if state.running:
            return ("ciclo em andamento", 409)

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    if device == "fan":
        GPIO.setup(FAN_PIN, GPIO.OUT)
        fan_write(on)
        with state.lock:
            state.manual_fan = on
            state.fan_active = on
    elif device == "base":
        GPIO.setup(SSR_BASE_PIN, GPIO.OUT)
        GPIO.output(SSR_BASE_PIN, GPIO.HIGH if on else GPIO.LOW)
        with state.lock:
            state.manual_base = on
    elif device == "gun":
        GPIO.setup(SSR_GUN_PIN, GPIO.OUT)
        GPIO.output(SSR_GUN_PIN, GPIO.HIGH if on else GPIO.LOW)
        with state.lock:
            state.manual_gun = on
    else:
        return ("device inválido", 400)

    return ("ok", 200)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Desativa tudo e desliga o Raspberry Pi."""
    with state.lock:
        state.running = False
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(SSR_BASE_PIN, GPIO.OUT)
        GPIO.setup(SSR_GUN_PIN,  GPIO.OUT)
        GPIO.setup(FAN_PIN,      GPIO.OUT)
        GPIO.setup(BUZZER_PIN,   GPIO.OUT)
        GPIO.output(SSR_BASE_PIN, GPIO.LOW)
        GPIO.output(SSR_GUN_PIN,  GPIO.LOW)
        GPIO.output(BUZZER_PIN,   GPIO.HIGH)  # desliga buzzer
        fan_write(False)
    except Exception:
        pass
    def do_shutdown():
        time.sleep(1)
        os.system("sudo shutdown -h now")
    threading.Thread(target=do_shutdown, daemon=True).start()
    return ("ok", 200)


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    import socket
    ip = socket.gethostbyname(socket.gethostname())
    print(f"\n  Acesse no browser: http://{ip}:5000\n")
    print("  Pinos:")
    print(f"    SSR Base:        GPIO {SSR_BASE_PIN}")
    print(f"    SSR Canhão:      GPIO {SSR_GUN_PIN}")
    print(f"    Ventilador:      GPIO {FAN_PIN}  (ativo em {'HIGH' if FAN_ACTIVE_HIGH else 'LOW'})")
    print(f"    Buzzer:          GPIO {BUZZER_PIN} (ativo em LOW)")
    print(f"    Termopar base:   SPI CE{CS_BASE} (GPIO 8)")
    print(f"    Termopar canhão: SPI CE{CS_GUN}  (GPIO 7)")
    print(f"    Display:         ESP32 externo via /display_data")
    print()

    sb = threading.Thread(target=standby_loop, daemon=True)
    sb.start()

    app.run(host="0.0.0.0", port=5000, threaded=True)