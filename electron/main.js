"use strict";

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { pythonCommand } = require("./python-command");

const APP_HOST = "127.0.0.1";
const DEFAULT_PORT = 8080;
const START_TIMEOUT_MS = 30000;

let mainWindow = null;
let backendProcess = null;
let backendStopped = false;
let isQuitting = false;
let backendUrl = null;
const backendLog = [];

function projectRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function packagedBackendExecutable(root) {
  const executable = process.platform === "win32" ? "pluto-adsb-backend.exe" : "pluto-adsb-backend";
  const candidates = [
    path.join(root, "backend", "pluto-adsb-backend", executable),
    path.join(root, "backend", executable),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function rememberBackendLog(line) {
  const text = String(line || "").trim();
  if (!text) return;
  backendLog.push(text);
  if (backendLog.length > 40) backendLog.shift();
  console.log(`[backend] ${text}`);
}

function trackerArgsFromCommandLine() {
  const markerIndex = process.argv.indexOf("--");
  const rawArgs = markerIndex >= 0 ? process.argv.slice(markerIndex + 1) : process.argv.slice(app.isPackaged ? 1 : 2);
  const output = [];

  for (let index = 0; index < rawArgs.length; index += 1) {
    const arg = rawArgs[index];
    if (arg === "--host" || arg === "--port" || arg === "--self-test") {
      if ((arg === "--host" || arg === "--port") && rawArgs[index + 1] && !rawArgs[index + 1].startsWith("--")) {
        index += 1;
      }
      continue;
    }
    if (arg.startsWith("--host=") || arg.startsWith("--port=")) continue;
    output.push(arg);
  }

  return output;
}

function getFreePort(preferredPort = DEFAULT_PORT) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", (error) => {
      if (preferredPort !== 0 && error.code === "EADDRINUSE") {
        getFreePort(0).then(resolve, reject);
      } else {
        reject(error);
      }
    });
    server.listen({ host: APP_HOST, port: preferredPort }, () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function waitForBackend(url, timeoutMs = START_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;

  return new Promise((resolve, reject) => {
    const poll = () => {
      if (!backendProcess || backendProcess.exitCode !== null) {
        reject(new Error(`Il backend Python si e chiuso prima dell'avvio.\n${backendLog.join("\n")}`));
        return;
      }

      const request = http.get(`${url}/api/status`, { timeout: 1200 }, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode >= 200 && response.statusCode < 500) {
          resolve();
          return;
        }
        retry();
      });

      request.on("timeout", () => {
        request.destroy();
        retry();
      });
      request.on("error", retry);
    };

    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error(`Timeout avvio backend Python.\n${backendLog.join("\n")}`));
        return;
      }
      setTimeout(poll, 250);
    };

    poll();
  });
}

async function startBackend() {
  const root = projectRoot();
  const portFromEnv = Number(process.env.PLUTO_ADSB_PORT || DEFAULT_PORT);
  const port = await getFreePort(Number.isFinite(portFromEnv) ? portFromEnv : DEFAULT_PORT);
  const packagedBackend = app.isPackaged ? packagedBackendExecutable(root) : null;
  let command = packagedBackend;
  let backendArgs = [
    ...trackerArgsFromCommandLine(),
    "--host",
    APP_HOST,
    "--port",
    String(port),
  ];
  let cwd = packagedBackend ? path.dirname(packagedBackend) : root;

  if (!command) {
    const scriptPath = path.join(root, "pluto_adsb_tracker.py");
    if (!fs.existsSync(scriptPath)) {
      throw new Error(`File backend non trovato: ${scriptPath}`);
    }
    const python = pythonCommand(root);
    command = python.command;
    backendArgs = [...python.args, scriptPath, ...backendArgs];
  }

  backendUrl = `http://${APP_HOST}:${port}`;
  backendProcess = spawn(command, backendArgs, {
    cwd,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
    },
    windowsHide: true,
  });

  backendProcess.stdout.on("data", (chunk) => rememberBackendLog(chunk.toString()));
  backendProcess.stderr.on("data", (chunk) => rememberBackendLog(chunk.toString()));
  backendProcess.on("error", (error) => rememberBackendLog(error.message));
  backendProcess.on("exit", (code, signal) => {
    backendStopped = true;
    rememberBackendLog(`processo terminato code=${code ?? "-"} signal=${signal ?? "-"}`);
    if (!isQuitting && mainWindow) {
      dialog.showErrorBox(
        "Backend interrotto",
        `Il server locale del tracker si e chiuso.\n\n${backendLog.slice(-8).join("\n")}`,
      );
    }
  });

  await waitForBackend(backendUrl);
  return backendUrl;
}

function isLocalBackendUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.origin === backendUrl;
  } catch (_error) {
    return false;
  }
}

function createWindow(url) {
  Menu.setApplicationMenu(null);

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 980,
    minHeight: 680,
    title: "Pluto ADS-B Tracker",
    backgroundColor: "#050607",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    if (isLocalBackendUrl(targetUrl)) return { action: "allow" };
    shell.openExternal(targetUrl);
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, targetUrl) => {
    if (isLocalBackendUrl(targetUrl)) return;
    event.preventDefault();
    shell.openExternal(targetUrl);
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  return mainWindow.loadURL(url);
}

function stopBackend() {
  return new Promise((resolve) => {
    if (!backendProcess || backendStopped) {
      resolve();
      return;
    }

    const child = backendProcess;
    const done = () => resolve();
    const timer = setTimeout(() => {
      if (!backendStopped) child.kill("SIGKILL");
      done();
    }, 3000);

    child.once("exit", () => {
      clearTimeout(timer);
      done();
    });
    child.kill();
  });
}

app.whenReady()
  .then(startBackend)
  .then(createWindow)
  .catch((error) => {
    dialog.showErrorBox("Avvio fallito", String(error && error.stack ? error.stack : error));
    app.quit();
  });

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", (event) => {
  if (isQuitting) return;
  event.preventDefault();
  isQuitting = true;
  stopBackend().finally(() => app.quit());
});
