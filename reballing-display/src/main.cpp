/*
 * Reballing Machine 1.0b — Dashboard + Menu ESP32 + ILI9488
 *
 * Telas:
 *   1. STANDBY  — titulo, sensores ao vivo, testes rapidos, botao "Carregar Perfil"
 *   2. PROFILE  — navegacao com setas entre os perfis, botao "Iniciar"
 *   3. ACTIVE   — etapa, temperaturas, duty, timer, pills das etapas
 *
 * Pinos (definidos no platformio.ini):
 *   CS=5  RST=4  DC=2  MOSI=23  SCK=18  MISO=19  Touch CS=15
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>
#include <SPI.h>

// ── Configuração ────────────────────────────────────────────
const char* WIFI_SSID = "wXBoxOne-TIM(2g)";
const char* WIFI_PASS = "txxtsbb@netwl";
const char* RASP_HOST = "http://192.168.1.250:5000";  // IP do Raspberry Pi

const unsigned long POLL_INTERVAL_MS = 1000;

// ── Display ──────────────────────────────────────────────────
TFT_eSPI tft = TFT_eSPI();

TFT_eSprite sprBase   = TFT_eSprite(&tft);
TFT_eSprite sprGun    = TFT_eSprite(&tft);
TFT_eSprite sprDutyB  = TFT_eSprite(&tft);
TFT_eSprite sprDutyG  = TFT_eSprite(&tft);
TFT_eSprite sprTimer  = TFT_eSprite(&tft);
TFT_eSprite sprTotal  = TFT_eSprite(&tft);
TFT_eSprite sprWifi   = TFT_eSprite(&tft);
TFT_eSprite sprGraph  = TFT_eSprite(&tft);   // mini-gráfico de temperatura

// Histórico curto em memória — só enquanto a tela ativa está aberta,
// não persiste, reseta quando muda de etapa ou volta ao standby
#define GRAPH_W 430
#define GRAPH_POINTS GRAPH_W   // 1 ponto por pixel de largura
float graphBase[GRAPH_POINTS];
float graphGun[GRAPH_POINTS];
int graphCount = 0;   // quantos pontos já temos preenchidos
int graphHead  = 0;   // próxima posição a escrever (buffer circular)

// Cores (RGB565)
#define COR_FUNDO      0x0841
#define COR_HEADER     0x1A11
#define COR_ROXO       0x955F
#define COR_VERMELHO   0xFBAE
#define COR_LARANJA    0xF9A2
#define COR_VERDE      0x6F6D
#define COR_AMARELO    0xFF49
#define COR_AZUL       0x3DDF
#define COR_CINZA      0x4A49
#define COR_CINZA_ESC  0x2104
#define COR_BRANCO     0xFFFF

#define TOUCH_DEBUG false

// ── Helper de acentos (declarado cedo — usado por varias funcoes) ──
String removeAccents(String s) {
  // A fonte padrao da TFT_eSPI nao tem acentos — substitui pelos
  // equivalentes sem acento para exibir corretamente.
  struct Repl { const char* from; const char* to; };
  static const Repl table[] = {
    {"á","a"},{"à","a"},{"â","a"},{"ã","a"},{"ä","a"},
    {"é","e"},{"è","e"},{"ê","e"},{"ë","e"},
    {"í","i"},{"ì","i"},{"î","i"},{"ï","i"},
    {"ó","o"},{"ò","o"},{"ô","o"},{"õ","o"},{"ö","o"},
    {"ú","u"},{"ù","u"},{"û","u"},{"ü","u"},
    {"ç","c"},{"ñ","n"},
    {"Á","A"},{"À","A"},{"Â","A"},{"Ã","A"},{"Ä","A"},
    {"É","E"},{"È","E"},{"Ê","E"},{"Ë","E"},
    {"Í","I"},{"Ì","I"},{"Î","I"},{"Ï","I"},
    {"Ó","O"},{"Ò","O"},{"Ô","O"},{"Õ","O"},{"Ö","O"},
    {"Ú","U"},{"Ù","U"},{"Û","U"},{"Ü","U"},
    {"Ç","C"},{"Ñ","N"},
  };
  for (auto &r : table) {
    s.replace(r.from, r.to);
  }
  return s;
}

// ── Estado recebido do Rasp (ciclo ativo) ─────────────────────
struct DisplayState {
  bool   running        = false;
  float  base_temp      = 0;
  float  gun_temp       = 0;
  float  base_duty      = 0;
  float  gun_duty       = 0;
  bool   gun_active     = false;
  bool   fan_active     = false;
  String step_name      = "Aguardando...";
  int    step_idx       = 0;
  int    step_remaining = 0;
  int    total_s        = 0;
  String error_msg      = "";
  String profile_label  = "";
  String profile_key    = "";
  String step_names[5];
  int    step_sps[5];
  int    step_durs[5];
  int    n_steps        = 0;
} state;

// ── Lista de perfis (carregada uma vez do Rasp) ────────────────
#define MAX_PROFILES 12
struct ProfileInfo {
  String key;
  String label;
  String group;
  int    gun_sp;
  String step_names[5];
  int    step_sps[5];
  int    step_durs[5];
  int    n_steps;
};
ProfileInfo profiles[MAX_PROFILES];
int n_profiles = 0;
bool profilesLoaded = false;
int selectedProfileIdx = 0;

// ── Máquina de estados de tela ──────────────────────────────────
enum AppScreen { SCR_BOOT, SCR_STANDBY, SCR_PROFILE, SCR_ACTIVE, SCR_WIFI_ERR, SCR_CONN_ERR };
AppScreen currentScreen = SCR_BOOT;
AppScreen lastDrawnScreen = SCR_BOOT;

int lastStepIdx = -1;
bool lastGunActive = false;
bool lastFanActive = false;
int lastProfileIdx = -1;

unsigned long lastPoll = 0;
bool wifiOk = false;

// Estado dos botões de teste rápido (tela standby)
bool fanButtonState  = false;
bool baseButtonState = false;
bool gunButtonState  = false;

// ── HTTP helpers ─────────────────────────────────────────────
String httpGet(String path) {
  if (WiFi.status() != WL_CONNECTED) return "";
  HTTPClient http;
  http.begin(String(RASP_HOST) + path);
  http.setTimeout(3000);
  int code = http.GET();
  String payload = "";
  if (code == 200) payload = http.getString();
  http.end();
  return payload;
}

bool httpPostJson(String path, String jsonBody) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.begin(String(RASP_HOST) + path);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(jsonBody);
  http.end();
  return code == 200;
}

void sendManual(String device, bool on) {
  String body = "{\"device\":\"" + device + "\",\"on\":" + (on ? "true" : "false") + "}";
  httpPostJson("/manual", body);
}

void sendStart(String profileKey) {
  String body = "{\"profile\":\"" + profileKey + "\"}";
  httpPostJson("/start", body);
}

void sendStop() {
  httpPostJson("/stop", "{}");
}

// ── WiFi ──────────────────────────────────────────────────────
void connectWiFi() {
  tft.fillScreen(COR_FUNDO);
  tft.setTextColor(COR_ROXO, COR_FUNDO);
  tft.setTextSize(2);
  tft.setCursor(20, 30);
  tft.println("REBALLING MACHINE");
  tft.setTextSize(1);
  tft.setCursor(20, 60);
  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.println("v1.0b");
  tft.setCursor(20, 90);
  tft.println("Conectando ao WiFi...");

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tentativas = 0;
  while (WiFi.status() != WL_CONNECTED && tentativas < 40) {
    delay(500);
    tft.print(".");
    tentativas++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifiOk = true;
    tft.setCursor(20, 110);
    tft.setTextColor(COR_VERDE, COR_FUNDO);
    tft.print("WiFi OK: ");
    tft.println(WiFi.localIP());
    delay(600);
  } else {
    wifiOk = false;
    tft.setCursor(20, 110);
    tft.setTextColor(COR_VERMELHO, COR_FUNDO);
    tft.println("Falha no WiFi!");
  }
  lastDrawnScreen = SCR_BOOT;  // força redesenho da próxima tela
}

// ── Carrega lista de perfis (uma vez, ou sob demanda) ────────
bool loadProfiles() {
  String payload = httpGet("/display_profiles");
  if (payload.length() == 0) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return false;

  n_profiles = 0;
  for (JsonObject p : doc.as<JsonArray>()) {
    if (n_profiles >= MAX_PROFILES) break;
    ProfileInfo &pi = profiles[n_profiles];
    pi.key    = String((const char*)(p["key"]    | ""));
    pi.label  = removeAccents(String((const char*)(p["label"]  | "")));
    pi.group  = removeAccents(String((const char*)(p["group"]  | "")));
    pi.gun_sp = p["gun_sp"] | 0;

    pi.n_steps = 0;
    JsonArray steps = p["steps"];
    for (JsonObject s : steps) {
      if (pi.n_steps >= 5) break;
      pi.step_names[pi.n_steps] = removeAccents(String((const char*)(s["name"] | "")));
      pi.step_sps[pi.n_steps]   = s["sp"]  | 0;
      pi.step_durs[pi.n_steps]  = s["dur"] | 0;
      pi.n_steps++;
    }
    n_profiles++;
  }

  profilesLoaded = (n_profiles > 0);
  return profilesLoaded;
}

// ── Busca dados de status do ciclo ────────────────────────────
bool fetchDisplayData() {
  String payload = httpGet("/display_data");
  if (payload.length() == 0) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return false;

  state.running          = doc["running"]        | false;
  state.base_temp        = doc["base_temp"]      | 0.0;
  state.gun_temp         = doc["gun_temp"]        | 0.0;
  state.base_duty        = doc["base_duty"]      | 0.0;
  state.gun_duty         = doc["gun_duty"]        | 0.0;
  state.gun_active       = doc["gun_active"]     | false;
  state.fan_active       = doc["fan_active"]     | false;
  state.step_name        = removeAccents(String((const char*)(doc["step"] | "Aguardando...")));
  state.step_idx         = doc["step_idx"]        | 0;
  state.step_remaining   = doc["step_remaining"]  | 0;
  state.total_s          = doc["total_s"]          | 0;
  state.error_msg        = String((const char*)(doc["error"] | ""));
  state.profile_label    = removeAccents(String((const char*)(doc["profile_label"] | "")));
  state.profile_key      = String((const char*)(doc["profile_key"] | ""));

  JsonArray steps = doc["steps"];
  state.n_steps = 0;
  for (JsonObject s : steps) {
    if (state.n_steps >= 5) break;
    state.step_names[state.n_steps] = removeAccents(String((const char*)(s["name"] | "")));
    state.step_sps[state.n_steps]   = s["sp"]  | 0;
    state.step_durs[state.n_steps]  = s["dur"] | 0;
    state.n_steps++;
  }

  return true;
}

// ── Helpers ──────────────────────────────────────────────────
String fmtTime(int s) {
  if (s <= 0) return "--:--";
  int mm = s / 60;
  int ss = s % 60;
  char buf[8];
  sprintf(buf, "%d:%02d", mm, ss);
  return String(buf);
}

void createValueSprites() {
  sprBase.setColorDepth(8);   sprBase.createSprite(140, 36);
  sprGun.setColorDepth(8);    sprGun.createSprite(140, 36);
  sprDutyB.setColorDepth(8);  sprDutyB.createSprite(120, 24);
  sprDutyG.setColorDepth(8);  sprDutyG.createSprite(120, 24);
  sprTimer.setColorDepth(8);  sprTimer.createSprite(100, 24);
  sprTotal.setColorDepth(8);  sprTotal.createSprite(80, 16);
  sprWifi.setColorDepth(8);   sprWifi.createSprite(10, 10);
  sprGraph.setColorDepth(8);  sprGraph.createSprite(GRAPH_W, 62);
}

// ════════════════════════════════════════════════════════════
// TELA 1 — STANDBY
// ════════════════════════════════════════════════════════════
// Layout:
//  Header: "REBALLING MACHINE 1.0b"
//  Sensores: BASE / CANHAO ao vivo
//  Botoes de teste rapido: Ventilador / SSR Base / SSR Canhao
//  Botao grande: "CARREGAR PERFIL"
// ════════════════════════════════════════════════════════════
void drawStandbyBackground() {
  tft.fillScreen(COR_FUNDO);

  tft.fillRect(0, 0, 480, 36, COR_HEADER);
  tft.setTextColor(COR_ROXO, COR_HEADER);
  tft.setTextSize(2);
  tft.setCursor(10, 8);
  tft.println("REBALLING MACHINE 1.0b");

  tft.drawFastHLine(0, 36, 480, COR_CINZA_ESC);

  tft.setTextSize(1);
  tft.setCursor(20, 48);
  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.println("BASE:");
  tft.setCursor(260, 48);
  tft.println("CANHAO:");

  tft.drawFastHLine(20, 120, 440, COR_CINZA_ESC);

  tft.setTextSize(1);
  tft.setCursor(20, 130);
  tft.setTextColor(COR_CINZA_ESC, COR_FUNDO);
  tft.println("Teste rapido:");

  // Botões de teste — 3 botões pequenos lado a lado
  int by = 150, bh = 50, bw = 140, gap = 10;
  tft.drawRoundRect(20,               by, bw, bh, 5, COR_CINZA);
  tft.drawRoundRect(20+bw+gap,        by, bw, bh, 5, COR_CINZA);
  tft.drawRoundRect(20+2*(bw+gap),    by, bw, bh, 5, COR_CINZA);

  tft.drawFastHLine(20, 220, 440, COR_CINZA_ESC);

  // Botão grande de carregar perfil
  tft.fillRoundRect(20, 235, 440, 60, 8, COR_ROXO);
  tft.setTextColor(COR_FUNDO, COR_ROXO);
  tft.setTextSize(2);
  tft.setCursor(120, 255);
  tft.println("CARREGAR PERFIL");
}

void updateStandbyValues() {
  sprBase.fillSprite(COR_FUNDO);
  sprBase.setTextSize(3);
  sprBase.setTextColor(COR_VERMELHO, COR_FUNDO);
  sprBase.setCursor(0, 0);
  sprBase.printf("%.1fC", state.base_temp);
  sprBase.pushSprite(20, 62);

  sprGun.fillSprite(COR_FUNDO);
  sprGun.setTextSize(3);
  sprGun.setTextColor(COR_LARANJA, COR_FUNDO);
  sprGun.setCursor(0, 0);
  sprGun.printf("%.1fC", state.gun_temp);
  sprGun.pushSprite(260, 62);

  // Botões de teste — preenche conforme estado
  int by = 150, bh = 50, bw = 140, gap = 10;
  int xs[3] = { 20, 20+bw+gap, 20+2*(bw+gap) };
  bool states_[3] = { fanButtonState, baseButtonState, gunButtonState };
  const char* labels[3] = { "VENTILADOR", "SSR BASE", "SSR CANHAO" };
  uint16_t cores[3] = { COR_AZUL, COR_VERMELHO, COR_LARANJA };

  for (int i = 0; i < 3; i++) {
    uint16_t bg = states_[i] ? cores[i] : COR_CINZA_ESC;
    uint16_t fg = states_[i] ? COR_FUNDO : COR_CINZA;
    tft.fillRoundRect(xs[i]+2, by+2, bw-4, bh-4, 4, bg);
    tft.drawRoundRect(xs[i], by, bw, bh, 5, states_[i] ? cores[i] : COR_CINZA);
    tft.setTextColor(fg, bg);
    tft.setTextSize(1);
    tft.setCursor(xs[i] + (bw - strlen(labels[i])*6)/2, by + bh/2 - 4);
    tft.print(labels[i]);
  }
}

// Botão de carregar perfil — bbox (20,235) a (460,295)
// Botões de teste — bbox conforme xs[i], by=150, bh=50, bw=140

// ════════════════════════════════════════════════════════════
// TELA 2 — SELEÇÃO DE PERFIL
// ════════════════════════════════════════════════════════════
void drawProfileBackground() {
  tft.fillScreen(COR_FUNDO);

  tft.fillRect(0, 0, 480, 40, COR_HEADER);
  tft.setTextColor(COR_ROXO, COR_HEADER);
  tft.setTextSize(1);
  tft.setCursor(10, 14);
  tft.println("SELECIONAR PERFIL");

  // Botão voltar — área grande e destacada (vermelho), fácil de acertar
  tft.fillRoundRect(395, 4, 80, 32, 5, COR_VERMELHO);
  tft.setTextColor(COR_FUNDO, COR_VERMELHO);
  tft.setTextSize(1);
  tft.setCursor(412, 14);
  tft.println("VOLTAR");

  tft.drawFastHLine(0, 40, 480, COR_CINZA_ESC);

  // Setas de navegação
  tft.fillTriangle(30, 165, 65, 135, 65, 195, COR_CINZA);   // seta esquerda
  tft.fillTriangle(450, 165, 415, 135, 415, 195, COR_CINZA); // seta direita

  // Botão iniciar
  tft.fillRoundRect(140, 270, 200, 40, 6, COR_VERDE);
  tft.setTextColor(COR_FUNDO, COR_VERDE);
  tft.setTextSize(2);
  tft.setCursor(195, 280);
  tft.println("INICIAR");
}

void drawProfileCard() {
  // Limpa a área central do card
  tft.fillRect(80, 40, 320, 215, COR_FUNDO);

  if (n_profiles == 0) {
    tft.setTextColor(COR_VERMELHO, COR_FUNDO);
    tft.setTextSize(1);
    tft.setCursor(100, 100);
    tft.println("Nenhum perfil carregado.");
    return;
  }

  ProfileInfo &p = profiles[selectedProfileIdx];

  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.setTextSize(1);
  tft.setCursor(90, 45);
  tft.printf("%s  (%d/%d)", p.group.c_str(), selectedProfileIdx+1, n_profiles);

  tft.setTextColor(COR_ROXO, COR_FUNDO);
  tft.setTextSize(2);
  tft.setCursor(90, 62);
  tft.println(p.label);

  tft.drawFastHLine(90, 90, 300, COR_CINZA_ESC);

  tft.setTextSize(1);
  int y = 100;
  for (int i = 0; i < p.n_steps; i++) {
    tft.setTextColor(COR_CINZA, COR_FUNDO);
    tft.setCursor(90, y);
    tft.print(p.step_names[i]);
    tft.setCursor(220, y);
    tft.setTextColor(COR_AMARELO, COR_FUNDO);
    tft.printf("%dC", p.step_sps[i]);
    tft.setCursor(280, y);
    tft.setTextColor(COR_CINZA, COR_FUNDO);
    if (p.step_durs[i] > 0) tft.printf("%ds", p.step_durs[i]);
    else tft.print("ate esfriar");
    y += 20;
  }

  tft.setCursor(90, y + 5);
  tft.setTextColor(COR_LARANJA, COR_FUNDO);
  tft.printf("Canhao: %dC (bang-bang)", p.gun_sp);

  lastProfileIdx = selectedProfileIdx;
}

// ════════════════════════════════════════════════════════════
// TELA 3 — CICLO ATIVO
// ════════════════════════════════════════════════════════════
void drawActiveBackground() {
  tft.fillScreen(COR_FUNDO);

  tft.fillRect(0, 0, 480, 40, COR_HEADER);
  tft.setTextColor(COR_ROXO, COR_HEADER);
  tft.setTextSize(1);
  tft.setCursor(8, 7);
  tft.println(state.step_name.substring(0, 30));

  tft.setCursor(8, 22);
  tft.setTextColor(COR_CINZA, COR_HEADER);
  tft.print("T:");

  // Botão PARAR — área grande e destacada, fácil de acertar
  tft.fillRoundRect(395, 4, 80, 32, 5, COR_VERMELHO);
  tft.setTextColor(COR_FUNDO, COR_VERMELHO);
  tft.setTextSize(1);
  tft.setCursor(415, 14);
  tft.println("PARAR");

  tft.drawFastHLine(0, 40, 480, COR_CINZA_ESC);

  // Temperaturas — faixa 40-118
  tft.setTextSize(1);
  tft.setCursor(12, 46);
  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.println("BASE");

  tft.drawFastVLine(238, 43, 72, COR_CINZA_ESC);

  tft.setCursor(250, 46);
  tft.println("CANHAO");

  tft.drawFastHLine(0, 118, 480, COR_CINZA_ESC);

  // Duty/timer — faixa 118-156
  tft.setCursor(12, 124);
  tft.println("Duty base");
  tft.setCursor(160, 124);
  tft.println("Duty canhao");
  tft.setCursor(340, 124);
  tft.println("Restante");

  tft.drawFastHLine(0, 156, 480, COR_CINZA_ESC);

  // Mini-grafico — faixa 156-218
  tft.setCursor(12, 160);
  tft.setTextColor(COR_CINZA_ESC, COR_FUNDO);
  tft.print("250C");
  tft.setCursor(12, 205);
  tft.print("0C");

  tft.drawFastHLine(0, 218, 480, COR_CINZA_ESC);

  // Reseta histórico do gráfico ao entrar na tela ativa
  graphCount = 0;
  graphHead  = 0;
}

void drawIndicators() {
  tft.fillRect(0, 226, 224, 86, COR_FUNDO);

  if (state.gun_active) {
    tft.drawRoundRect(8, 226, 100, 76, 4, COR_LARANJA);
    tft.setTextColor(COR_LARANJA, COR_FUNDO);
    tft.setTextSize(1);
    tft.setCursor(18, 240);
    tft.println("CANHAO");
    tft.setCursor(18, 262);
    tft.println("ATIVO");
  }
  if (state.fan_active) {
    tft.drawRoundRect(116, 226, 100, 76, 4, COR_AZUL);
    tft.setTextColor(COR_AZUL, COR_FUNDO);
    tft.setTextSize(1);
    tft.setCursor(126, 240);
    tft.println("VENT.");
    tft.setCursor(126, 262);
    tft.println("ATIVO");
  }
  lastGunActive = state.gun_active;
  lastFanActive = state.fan_active;
}

void drawStepPills() {
  tft.fillRect(224, 226, 256, 86, COR_FUNDO);
  int x = 224;
  for (int i = 0; i < state.n_steps; i++) {
    uint16_t cor;
    if (i < state.step_idx) cor = COR_VERDE;
    else if (i == state.step_idx) cor = COR_ROXO;
    else cor = COR_CINZA_ESC;

    tft.drawRoundRect(x, 228, 44, 72, 3, cor);
    tft.setTextColor(cor, COR_FUNDO);
    tft.setTextSize(1);
    tft.setCursor(x+3, 234);
    tft.println(state.step_names[i].substring(0, 5));
    tft.setCursor(x+3, 254);
    tft.printf("%dC", state.step_sps[i]);
    tft.setCursor(x+3, 274);
    if (state.step_durs[i] > 0) tft.printf("%ds", state.step_durs[i]);
    else tft.print("v");

    x += 50;
  }
  lastStepIdx = state.step_idx;
}

// Desenha o mini-gráfico de temperatura — faixa y=156 a y=218 (altura 62px)
// Escala fixa 0-250°C, sem eixos dinâmicos — simples e leve.
void updateGraph() {
  const int GY0 = 156, GY1 = 218;   // topo e base da faixa do gráfico
  const int GX0 = 0;                 // x onde o gráfico começa
  const float TMAX = 250.0;

  sprGraph.fillSprite(COR_FUNDO);

  // Linha de base (0°C) e topo sutil
  sprGraph.drawFastHLine(0, (GY1-GY0)-1, GRAPH_W, COR_CINZA_ESC);

  // Adiciona o ponto atual no buffer circular
  if (graphCount < GRAPH_POINTS) {
    graphBase[graphHead] = state.base_temp;
    graphGun[graphHead]  = state.gun_temp;
    graphHead = (graphHead + 1) % GRAPH_POINTS;
    graphCount++;
  } else {
    // Buffer cheio — desloca tudo um ponto para a esquerda (scroll)
    for (int i = 0; i < GRAPH_POINTS - 1; i++) {
      graphBase[i] = graphBase[i+1];
      graphGun[i]  = graphGun[i+1];
    }
    graphBase[GRAPH_POINTS-1] = state.base_temp;
    graphGun[GRAPH_POINTS-1]  = state.gun_temp;
  }

  int n = graphCount;
  int h = GY1 - GY0;

  // Desenha linha da base (vermelho)
  for (int i = 1; i < n; i++) {
    int x0 = i - 1, x1 = i;
    int y0 = h - (int)((graphBase[x0] / TMAX) * h);
    int y1 = h - (int)((graphBase[x1] / TMAX) * h);
    y0 = constrain(y0, 0, h-1);
    y1 = constrain(y1, 0, h-1);
    sprGraph.drawLine(x0, y0, x1, y1, COR_VERMELHO);
  }

  // Desenha linha do canhão (laranja) — só se ativo em algum ponto
  for (int i = 1; i < n; i++) {
    int x0 = i - 1, x1 = i;
    int y0 = h - (int)((graphGun[x0] / TMAX) * h);
    int y1 = h - (int)((graphGun[x1] / TMAX) * h);
    y0 = constrain(y0, 0, h-1);
    y1 = constrain(y1, 0, h-1);
    sprGraph.drawLine(x0, y0, x1, y1, COR_LARANJA);
  }

  sprGraph.pushSprite(GX0 + 40, GY0);
}

void updateActiveValues() {
  sprBase.fillSprite(COR_FUNDO);
  sprBase.setTextSize(3);
  sprBase.setTextColor(COR_VERMELHO, COR_FUNDO);
  sprBase.setCursor(0, 0);
  sprBase.printf("%.1fC", state.base_temp);
  sprBase.pushSprite(12, 60);

  sprGun.fillSprite(COR_FUNDO);
  sprGun.setTextSize(3);
  sprGun.setTextColor(state.gun_active ? COR_LARANJA : COR_CINZA, COR_FUNDO);
  sprGun.setCursor(0, 0);
  sprGun.printf("%.1fC", state.gun_temp);
  sprGun.pushSprite(250, 60);

  sprDutyB.fillSprite(COR_FUNDO);
  sprDutyB.setTextSize(2);
  sprDutyB.setTextColor(COR_VERDE, COR_FUNDO);
  sprDutyB.setCursor(0, 0);
  sprDutyB.printf("%.1f%%", state.base_duty);
  sprDutyB.pushSprite(12, 138);

  sprDutyG.fillSprite(COR_FUNDO);
  sprDutyG.setTextSize(2);
  sprDutyG.setCursor(0, 0);
  if (state.gun_active) {
    sprDutyG.setTextColor(COR_LARANJA, COR_FUNDO);
    sprDutyG.printf("%.1f%%", state.gun_duty);
  } else {
    sprDutyG.setTextColor(COR_CINZA, COR_FUNDO);
    sprDutyG.print("--");
  }
  sprDutyG.pushSprite(160, 138);

  sprTimer.fillSprite(COR_FUNDO);
  sprTimer.setTextSize(2);
  sprTimer.setTextColor(COR_AMARELO, COR_FUNDO);
  sprTimer.setCursor(0, 0);
  sprTimer.println(fmtTime(state.step_remaining));
  sprTimer.pushSprite(340, 138);

  sprTotal.fillSprite(COR_HEADER);
  sprTotal.setTextSize(1);
  sprTotal.setTextColor(COR_CINZA, COR_HEADER);
  sprTotal.setCursor(0, 0);
  sprTotal.println(fmtTime(state.total_s));
  sprTotal.pushSprite(24, 22);

  sprWifi.fillSprite(COR_HEADER);
  sprWifi.fillCircle(5, 5, 3, wifiOk ? COR_VERDE : COR_VERMELHO);
  sprWifi.pushSprite(375, 8);

  updateGraph();

  if (state.gun_active != lastGunActive || state.fan_active != lastFanActive) {
    drawIndicators();
  }
  if (state.step_idx != lastStepIdx) {
    drawStepPills();
  }

  // Se o ciclo terminou (servidor reporta running=false), volta pro standby
  if (!state.running) {
    currentScreen = SCR_STANDBY;
  }
}

// ── TELAS DE ERRO ────────────────────────────────────────────
void drawWifiError() {
  tft.fillScreen(COR_FUNDO);
  tft.setTextColor(COR_VERMELHO, COR_FUNDO);
  tft.setTextSize(2);
  tft.setCursor(20, 30);
  tft.println("WiFi desconectado");
  tft.setTextSize(1);
  tft.setCursor(20, 70);
  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.println("Reconectando...");
  tft.setCursor(20, 90);
  tft.print("Rede: ");
  tft.println(WIFI_SSID);
}

void drawConnError() {
  tft.fillScreen(COR_FUNDO);
  tft.setTextColor(COR_VERMELHO, COR_FUNDO);
  tft.setTextSize(2);
  tft.setCursor(20, 30);
  tft.println("Sem resposta do Rasp");
  tft.setTextSize(1);
  tft.setCursor(20, 70);
  tft.setTextColor(COR_VERDE, COR_FUNDO);
  tft.print("WiFi OK: ");
  tft.println(WiFi.localIP());
  tft.setCursor(20, 90);
  tft.setTextColor(COR_CINZA, COR_FUNDO);
  tft.println("Verificando o servidor...");
  tft.setCursor(20, 110);
  tft.println(String(RASP_HOST) + "/display_data");
}

// ── Orquestração de telas ────────────────────────────────────
void showStandby() {
  if (lastDrawnScreen != SCR_STANDBY) {
    drawStandbyBackground();
    lastDrawnScreen = SCR_STANDBY;
  }
  updateStandbyValues();
}

void showProfile() {
  if (lastDrawnScreen != SCR_PROFILE) {
    drawProfileBackground();
    lastProfileIdx = -1;
    lastDrawnScreen = SCR_PROFILE;
  }
  if (selectedProfileIdx != lastProfileIdx) {
    drawProfileCard();
  }
}

void showActive() {
  if (lastDrawnScreen != SCR_ACTIVE) {
    drawActiveBackground();
    lastStepIdx = -1;
    lastGunActive = !state.gun_active;
    lastFanActive = !state.fan_active;
    lastDrawnScreen = SCR_ACTIVE;
  }
  updateActiveValues();
}

// ── Touch ────────────────────────────────────────────────────
void checkTouch() {
  uint16_t tx, ty;
  if (!tft.getTouch(&tx, &ty)) return;

  // Corrige inversão de eixos causada pela rotação do display
  tx = 480 - tx;
  ty = 320 - ty;

#if TOUCH_DEBUG
  Serial.printf("Touch -> x=%d y=%d  tela=%d\n", tx, ty, currentScreen);
  delay(150);
  return;
#endif

  if (currentScreen == SCR_STANDBY) {
    int by = 150, bh = 50, bw = 140, gap = 10;
    int xs[3] = { 20, 20+bw+gap, 20+2*(bw+gap) };

    // Botões de teste rápido
    for (int i = 0; i < 3; i++) {
      if (tx >= xs[i] && tx <= xs[i]+bw && ty >= by && ty <= by+bh) {
        if (i == 0) { fanButtonState  = !fanButtonState;  sendManual("fan",  fanButtonState); }
        if (i == 1) { baseButtonState = !baseButtonState; sendManual("base", baseButtonState); }
        if (i == 2) { gunButtonState  = !gunButtonState;  sendManual("gun",  gunButtonState); }
        delay(250);
        return;
      }
    }

    // Botão "Carregar Perfil"
    if (tx >= 20 && tx <= 460 && ty >= 235 && ty <= 295) {
      tft.fillScreen(COR_FUNDO);
      tft.setTextColor(COR_CINZA, COR_FUNDO);
      tft.setTextSize(2);
      tft.setCursor(100, 140);
      tft.println("Carregando perfis...");
      bool ok = loadProfiles();
      if (ok) {
        currentScreen = SCR_PROFILE;
        selectedProfileIdx = 0;
      } else {
        tft.setCursor(100, 180);
        tft.setTextColor(COR_VERMELHO, COR_FUNDO);
        tft.println("Falha ao carregar. Tente novamente.");
        delay(1500);
      }
      lastDrawnScreen = SCR_BOOT;  // força redesenho
      delay(200);
    }
  }
  else if (currentScreen == SCR_PROFILE) {
    // Botão voltar — hitbox com margem extra ao redor do retângulo desenhado (395,4)-(475,36)
    if (tx >= 385 && tx <= 480 && ty >= 0 && ty <= 44) {
      currentScreen = SCR_STANDBY;
      lastDrawnScreen = SCR_BOOT;
      delay(250);
      return;
    }
    // Seta esquerda — área grande ao redor do triângulo (30,165)-(65,135/195)
    if (tx >= 5 && tx <= 100 && ty >= 110 && ty <= 220) {
      if (n_profiles > 0) {
        selectedProfileIdx = (selectedProfileIdx - 1 + n_profiles) % n_profiles;
      }
      delay(200);
      return;
    }
    // Seta direita — área grande ao redor do triângulo (450,165)-(415,135/195)
    if (tx >= 380 && tx <= 475 && ty >= 110 && ty <= 220) {
      if (n_profiles > 0) {
        selectedProfileIdx = (selectedProfileIdx + 1) % n_profiles;
      }
      delay(200);
      return;
    }
    // Botão iniciar
    if (tx >= 140 && tx <= 340 && ty >= 270 && ty <= 310) {
      if (n_profiles > 0) {
        sendStart(profiles[selectedProfileIdx].key);
        currentScreen = SCR_ACTIVE;
        lastDrawnScreen = SCR_BOOT;
      }
      delay(300);
      return;
    }
  }
  else if (currentScreen == SCR_ACTIVE) {
    // Botão "PARAR" no header — hitbox com margem extra ao redor do retângulo (395,4)-(475,36)
    if (tx >= 385 && tx <= 480 && ty >= 0 && ty <= 44) {
      sendStop();
      delay(300);
      return;
    }
  }
}

// ── SETUP ────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  tft.init();
  tft.setRotation(1);
  tft.fillScreen(COR_FUNDO);

  uint16_t calData[5] = { 200, 3900, 200, 3900, 1 };
  tft.setTouch(calData);

  createValueSprites();
  Serial.println("Sprites criados.");

  connectWiFi();
  currentScreen = SCR_STANDBY;
}

// ── LOOP ─────────────────────────────────────────────────────
void loop() {
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    wifiOk = false;
    if (lastDrawnScreen != SCR_WIFI_ERR) {
      drawWifiError();
      lastDrawnScreen = SCR_WIFI_ERR;
    }
    connectWiFi();
    currentScreen = SCR_STANDBY;
    lastDrawnScreen = SCR_BOOT;
    return;
  }

  checkTouch();

  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;

    if (currentScreen == SCR_STANDBY || currentScreen == SCR_ACTIVE) {
      bool ok = fetchDisplayData();
      if (!ok) {
        if (lastDrawnScreen != SCR_CONN_ERR) {
          drawConnError();
          lastDrawnScreen = SCR_CONN_ERR;
        }
      } else {
        // Sincroniza tela com o estado real do servidor
        if (state.running && currentScreen != SCR_ACTIVE) {
          currentScreen = SCR_ACTIVE;
          lastDrawnScreen = SCR_BOOT;
        }
        if (currentScreen == SCR_ACTIVE) showActive();
        else showStandby();
      }
    }
    else if (currentScreen == SCR_PROFILE) {
      showProfile();
    }
  }
}