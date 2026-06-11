"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");
const { pythonCommand } = require("./python-command");

const root = path.resolve(__dirname, "..");
const python = pythonCommand(root);
const scriptPath = path.join(root, "pluto_adsb_tracker.py");

const child = spawn(python.command, [...python.args, scriptPath, ...process.argv.slice(2)], {
  cwd: root,
  env: {
    ...process.env,
    PYTHONUNBUFFERED: "1",
  },
  stdio: "inherit",
  windowsHide: false,
});

child.on("error", (error) => {
  console.error(error.message);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.exit(1);
    return;
  }
  process.exit(code ?? 0);
});
