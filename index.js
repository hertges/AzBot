const { spawn } = require("child_process");
const path = require("path");

const child = spawn("sh", [path.join(__dirname, "startup.sh")], {
  cwd: __dirname,
  stdio: "inherit",
});

child.on("exit", (code) => process.exit(code ?? 1));
process.on("SIGINT", () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
