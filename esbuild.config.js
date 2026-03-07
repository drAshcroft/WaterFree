// esbuild.config.js — VS Code extension bundler
const esbuild = require("esbuild");

const watch = process.argv.includes("--watch");
const minify = process.argv.includes("--minify");

/** @type {import('esbuild').BuildOptions} */
const config = {
  entryPoints: ["src/extension.ts"],
  bundle: true,
  // vscode is provided by the extension host — never bundle it
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node18",
  outfile: "dist/extension.js",
  sourcemap: !minify,
  minify,
  logLevel: "info",
};

if (watch) {
  esbuild.context(config).then((ctx) => {
    ctx.watch();
    console.log("Watching for changes...");
  });
} else {
  esbuild.build(config).catch(() => process.exit(1));
}
