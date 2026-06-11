"use strict";

const fs = require("node:fs");
const path = require("node:path");

function pythonCommand(root) {
  if (process.env.PLUTO_ADSB_PYTHON) {
    return { command: process.env.PLUTO_ADSB_PYTHON, args: [] };
  }

  const venvPython = process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");

  if (fs.existsSync(venvPython)) {
    return { command: venvPython, args: [] };
  }

  if (process.platform === "win32") {
    return { command: "py", args: ["-3"] };
  }

  return { command: "python3", args: [] };
}

module.exports = { pythonCommand };
