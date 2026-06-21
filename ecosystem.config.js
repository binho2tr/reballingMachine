module.exports = {
  apps: [{
    name: "reballing",
    script: "./venv/bin/python3",
    args: "reballing.py",
    cwd: "/home/spawn/reballing",

    // GPIO requer root — roda o processo como root
    // (alternativa sem sudo: usar grupo 'gpio' + udev rules, ver nota abaixo)
    interpreter: "none",

    autorestart: true,
    watch: false,
    max_restarts: 5,
    restart_delay: 3000,

    env: {
      PYTHONUNBUFFERED: "1"
    },

    out_file: "./logs/pm2-out.log",
    error_file: "./logs/pm2-error.log",
    merge_logs: true,
    time: true
  }]
};