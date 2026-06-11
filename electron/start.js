"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");
const electronPath = require("electron");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronPath, [path.resolve(__dirname, ".."), ...process.argv.slice(2)], {
  env,
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
  }
  process.exit(code ?? 0);
});
