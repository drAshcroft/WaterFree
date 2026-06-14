using System.Diagnostics;
using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace WaterFreeInstallerHelper;

internal static class Program
{
    // Historical MCP server names. We don't register these any more (the
    // backend exposes its functionality through `waterfree <area> <action>`
    // subcommands on PATH instead), but we still strip them from existing
    // Claude / Codex config files during uninstall so users don't end up with
    // dead entries.
    private static readonly string[] LegacyServers =
    {
        "pairprogram-debug",
        "pairprogram-index",
        "pairprogram-knowledge",
        "pairprogram-todos",
        "waterfree-debug",
        "waterfree-index",
        "waterfree-knowledge",
        "waterfree-todos",
        "waterfree-testing",
        "waterfree-qa-summary"
    };

    private static int Main(string[] args)
    {
        var installDir = GetArgValue(args, "--install-dir");
        var vsixPath = GetArgValue(args, "--vsix");
        var logPath = GetArgValue(args, "--log");
        var uninstall = HasArg(args, "--uninstall");
        if (string.IsNullOrWhiteSpace(installDir))
        {
            Console.Error.WriteLine("Missing --install-dir.");
            return 2;
        }

        installDir = Path.GetFullPath(installDir);
        var log = new InstallerLog(installDir, logPath);
        log.Info($"Start {(uninstall ? "uninstall" : "install")} helper. installDir={installDir}");

        try
        {
            if (uninstall)
            {
                // Clean up any legacy MCP entries left by previous versions.
                RemoveCodexConfig(log);
                RemoveClaudeConfig(log);
                RemoveInstalledSkills(log, installDir);
                RemoveExtractedRuntime(log, installDir);
                log.Info("Uninstall cleanup complete.");
                return 0;
            }

            var exePath = Path.Combine(installDir, "bin", "waterfree.exe");
            ExtractRuntime(log, installDir);

            // Newer installs do not register MCP servers — the CLI is invoked
            // directly via PATH. We still strip any legacy entries in case the
            // user is upgrading from an older WaterFree.
            RemoveCodexConfig(log);
            RemoveClaudeConfig(log);

            InstallSkills(log, installDir);
            InstallVsixIfPossible(log, vsixPath);
            SmokeTest(log, exePath);

            log.Info("Install helper completed successfully.");
            return 0;
        }
        catch (Exception ex)
        {
            log.Error($"Installer helper failed: {ex}");
            // The helper runs as a deferred MSI custom action — its console
            // window only flashes for a moment, so on failure show a blocking
            // dialog the user can actually read before the MSI rolls back.
            ShowErrorDialog(
                "WaterFree setup could not finish.\n\n" +
                ex.Message +
                "\n\nFull details were written to:\n" + log.LogPath +
                "\n\nIf a previous version is still running (VS Code, or a " +
                "'waterfree' window), close it and run setup again.");
            return 1;
        }
        finally
        {
            log.Dispose();
        }
    }

    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    private static void ShowErrorDialog(string message)
    {
        // Best-effort: never let a UI failure mask the original error.
        try
        {
            const uint MB_OK = 0x0;
            const uint MB_ICONERROR = 0x10;
            const uint MB_SETFOREGROUND = 0x10000;
            const uint MB_TOPMOST = 0x40000;
            MessageBoxW(IntPtr.Zero, message, "WaterFree Setup", MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST);
        }
        catch
        {
            // ignore — the error is already in the log and on the console
        }
    }

    private static void RemoveCodexConfig(InstallerLog log)
    {
        var codexDir = Path.Combine(GetUserHome(), ".codex");
        var configPath = Path.Combine(codexDir, "config.toml");
        if (!File.Exists(configPath))
        {
            log.Info("Codex config not found; skipping.");
            return;
        }

        var removeNames = LegacyServers.ToHashSet(StringComparer.OrdinalIgnoreCase);
        var lines = File.ReadAllLines(configPath).ToList();
        var updated = RemoveTomlSections(lines, removeNames);
        File.WriteAllLines(configPath, updated, new UTF8Encoding(false));
        log.Info($"Codex MCP legacy entries cleaned: {configPath}");
    }

    private static List<string> RemoveTomlSections(List<string> lines, HashSet<string> removeNames)
    {
        var result = new List<string>(lines.Count);
        var skipping = false;

        foreach (var line in lines)
        {
            var trimmed = line.Trim();
            if (trimmed.StartsWith("[") && trimmed.EndsWith("]"))
            {
                skipping = false;
                var header = trimmed.Substring(1, trimmed.Length - 2);
                if (header.StartsWith("mcp_servers.", StringComparison.OrdinalIgnoreCase))
                {
                    var name = header.Substring("mcp_servers.".Length);
                    if (removeNames.Contains(name))
                    {
                        skipping = true;
                        continue;
                    }
                }
            }

            if (!skipping)
            {
                result.Add(line);
            }
        }

        // Trim trailing empty lines for cleaner output.
        while (result.Count > 0 && string.IsNullOrWhiteSpace(result[^1]))
        {
            result.RemoveAt(result.Count - 1);
        }

        return result;
    }

    private static void RemoveClaudeConfig(InstallerLog log)
    {
        var configPath = Path.Combine(GetUserHome(), ".claude.json");
        if (!File.Exists(configPath))
        {
            log.Info("Claude config not found; skipping.");
            return;
        }

        JsonNode? rootNode;
        try
        {
            rootNode = JsonNode.Parse(File.ReadAllText(configPath));
        }
        catch
        {
            log.Info("Claude config parse failed; skipping removal.");
            return;
        }

        if (rootNode is not JsonObject root)
        {
            log.Info("Claude config root not object; skipping removal.");
            return;
        }

        var mcpServersNode = root["mcpServers"];
        if (mcpServersNode is JsonArray array)
        {
            var updated = new JsonArray();
            foreach (var item in array)
            {
                if (item is not JsonObject obj)
                {
                    updated.Add(item);
                    continue;
                }
                var name = obj["name"]?.GetValue<string>() ?? string.Empty;
                if (ShouldKeepServer(name))
                {
                    updated.Add(obj);
                }
            }
            root["mcpServers"] = updated;
        }
        else if (mcpServersNode is JsonObject obj)
        {
            foreach (var name in LegacyServers)
            {
                obj.Remove(name);
            }
            root["mcpServers"] = obj;
        }

        var options = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(configPath, root.ToJsonString(options), new UTF8Encoding(false));
        log.Info($"Claude MCP legacy entries cleaned: {configPath}");
    }

    private static void InstallSkills(InstallerLog log, string installDir)
    {
        var sourceRoot = Path.Combine(installDir, "skills");
        if (!Directory.Exists(sourceRoot))
        {
            log.Info($"Skills payload not found; skipping skill install: {sourceRoot}");
            return;
        }

        var packages = Directory.GetDirectories(sourceRoot)
            .Where(dir => File.Exists(Path.Combine(dir, "SKILL.md")))
            .OrderBy(Path.GetFileName, StringComparer.OrdinalIgnoreCase)
            .ToArray();

        if (packages.Length == 0)
        {
            log.Info($"No skill packages found under {sourceRoot}; skipping.");
            return;
        }

        InstallSkillsTo(log, packages, Path.Combine(GetUserHome(), ".codex", "skills"), "Codex");
        InstallSkillsTo(log, packages, Path.Combine(GetUserHome(), ".claude", "skills"), "Claude");
    }

    private static void InstallSkillsTo(
        InstallerLog log,
        string[] packages,
        string destinationRoot,
        string targetName)
    {
        Directory.CreateDirectory(destinationRoot);
        var installed = 0;

        foreach (var package in packages)
        {
            var name = Path.GetFileName(package);
            if (string.IsNullOrWhiteSpace(name)) continue;

            var destination = Path.Combine(destinationRoot, name);
            if (Directory.Exists(destination))
            {
                Directory.Delete(destination, recursive: true);
            }

            CopyDirectory(package, destination);
            installed++;
        }

        log.Info($"Installed {installed} WaterFree skill(s) for {targetName}: {destinationRoot}");
    }

    private static void RemoveInstalledSkills(InstallerLog log, string installDir)
    {
        var names = GetInstalledSkillNames(installDir);
        RemoveInstalledSkillsFrom(log, Path.Combine(GetUserHome(), ".codex", "skills"), "Codex", names);
        RemoveInstalledSkillsFrom(log, Path.Combine(GetUserHome(), ".claude", "skills"), "Claude", names);
    }

    private static string[] GetInstalledSkillNames(string installDir)
    {
        var sourceRoot = Path.Combine(installDir, "skills");
        if (Directory.Exists(sourceRoot))
        {
            var names = Directory.GetDirectories(sourceRoot)
                .Where(dir => File.Exists(Path.Combine(dir, "SKILL.md")))
                .Select(Path.GetFileName)
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Cast<string>()
                .ToArray();
            if (names.Length > 0) return names;
        }

        return new[]
        {
            "tutorialize",
            "waterfree-index",
            "waterfree-knowledge",
            "waterfree-qa-summary",
            "waterfree-testing",
            "waterfree-todos"
        };
    }

    private static void RemoveInstalledSkillsFrom(
        InstallerLog log,
        string destinationRoot,
        string targetName,
        string[] names)
    {
        if (!Directory.Exists(destinationRoot))
        {
            log.Info($"{targetName} skills directory not found; skipping removal.");
            return;
        }

        var removed = 0;
        foreach (var name in names)
        {
            var destination = Path.Combine(destinationRoot, name);
            if (!Directory.Exists(destination)) continue;
            Directory.Delete(destination, recursive: true);
            removed++;
        }

        log.Info($"Removed {removed} WaterFree skill(s) from {targetName}: {destinationRoot}");
    }

    private static void ExtractRuntime(InstallerLog log, string installDir)
    {
        var binDir = Path.Combine(installDir, "bin");
        var runtimeZip = Path.Combine(binDir, "waterfree-runtime.zip");
        var exePath = Path.Combine(binDir, "waterfree.exe");

        if (!File.Exists(runtimeZip))
        {
            throw new FileNotFoundException($"Runtime zip not found: {runtimeZip}", runtimeZip);
        }

        RemoveExtractedRuntime(log, installDir);
        ZipFile.ExtractToDirectory(runtimeZip, binDir, overwriteFiles: true);

        if (!File.Exists(exePath))
        {
            throw new FileNotFoundException($"Executable not found after runtime extraction: {exePath}", exePath);
        }

        log.Info($"Extracted WaterFree runtime into {binDir}");
    }

    private static void RemoveExtractedRuntime(InstallerLog log, string installDir)
    {
        var binDir = Path.Combine(installDir, "bin");
        if (!Directory.Exists(binDir)) return;

        // Free any locks first: on upgrade/repair a running waterfree.exe (the
        // VS Code backend's `serve`, or an open shell) holds the launcher image,
        // so the delete below — and the overwrite extract that follows — would
        // throw "used by another process" and roll back the whole MSI.
        StopRunningRuntime(log, binDir);

        // On re-install/upgrade the previous runtime may contain files that are
        // read-only or transiently locked (e.g. libcrypto-3.dll held by AV or a
        // running `waterfree serve`). A hard Directory.Delete throws
        // UnauthorizedAccessException and rolls back the whole MSI. Delete
        // best-effort instead; ExtractRuntime re-extracts with overwrite, so a
        // few leftover files are harmless.
        var failures = new List<string>();

        foreach (var directory in Directory.GetDirectories(binDir))
        {
            if (!TryForceDeleteDirectory(directory))
            {
                failures.Add(directory);
            }
        }

        foreach (var file in Directory.GetFiles(binDir))
        {
            var name = Path.GetFileName(file);
            if (name.Equals("waterfree-installer-helper.exe", StringComparison.OrdinalIgnoreCase)) continue;
            if (name.Equals("waterfree-runtime.zip", StringComparison.OrdinalIgnoreCase)) continue;
            if (!TryForceDeleteFile(file))
            {
                failures.Add(file);
            }
        }

        if (failures.Count > 0)
        {
            log.Info($"Could not remove {failures.Count} old runtime file(s) (locked or in use); " +
                     $"they will be overwritten on extract. First: {failures[0]}");
        }
        else
        {
            log.Info($"Removed extracted WaterFree runtime files from {binDir}");
        }
    }

    private static void StopRunningRuntime(InstallerLog log, string binDir)
    {
        // Terminate any waterfree launcher (and its child python) running *from
        // this install's bin dir* so its files can be replaced. Scoped by path so
        // an unrelated 'waterfree' elsewhere on the machine is never touched.
        Process[] procs;
        try
        {
            procs = Process.GetProcessesByName("waterfree");
        }
        catch (Exception ex)
        {
            log.Info($"Could not enumerate running runtime processes: {ex.Message}");
            return;
        }

        var prefix = binDir.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var stopped = 0;
        foreach (var proc in procs)
        {
            try
            {
                string? exePath = null;
                try { exePath = proc.MainModule?.FileName; }
                catch { /* access denied or already exited — skip */ }
                if (string.IsNullOrEmpty(exePath)) continue;
                if (!exePath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) continue;

                log.Info($"Stopping running runtime: {exePath} (pid={proc.Id}) so its files can be replaced.");
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(5000);
                stopped++;
            }
            catch (Exception ex)
            {
                log.Info($"Could not stop runtime pid={proc.Id}: {ex.Message}");
            }
            finally
            {
                proc.Dispose();
            }
        }

        if (stopped > 0)
        {
            log.Info($"Stopped {stopped} running runtime process(es) under {binDir}.");
        }
    }

    private static bool TryForceDeleteDirectory(string path)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                ClearReadOnlyRecursive(path);
                Directory.Delete(path, recursive: true);
                return true;
            }
            catch (Exception) when (attempt < 2)
            {
                Thread.Sleep(200);
            }
            catch (Exception)
            {
                return false;
            }
        }
        return false;
    }

    private static bool TryForceDeleteFile(string path)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                ClearReadOnly(path);
                File.Delete(path);
                return true;
            }
            catch (Exception) when (attempt < 2)
            {
                Thread.Sleep(200);
            }
            catch (Exception)
            {
                return false;
            }
        }
        return false;
    }

    private static void ClearReadOnlyRecursive(string directory)
    {
        foreach (var file in Directory.EnumerateFiles(directory, "*", SearchOption.AllDirectories))
        {
            ClearReadOnly(file);
        }
    }

    private static void ClearReadOnly(string path)
    {
        try
        {
            var attrs = File.GetAttributes(path);
            if ((attrs & FileAttributes.ReadOnly) == FileAttributes.ReadOnly)
            {
                File.SetAttributes(path, attrs & ~FileAttributes.ReadOnly);
            }
        }
        catch
        {
            // best-effort — the delete attempt will surface any real problem
        }
    }

    private static void CopyDirectory(string source, string destination)
    {
        Directory.CreateDirectory(destination);

        foreach (var file in Directory.GetFiles(source))
        {
            File.Copy(file, Path.Combine(destination, Path.GetFileName(file)), overwrite: true);
        }

        foreach (var directory in Directory.GetDirectories(source))
        {
            CopyDirectory(directory, Path.Combine(destination, Path.GetFileName(directory)));
        }
    }

    private static bool ShouldKeepServer(string name)
    {
        if (string.IsNullOrWhiteSpace(name)) return true;
        if (LegacyServers.Contains(name, StringComparer.OrdinalIgnoreCase)) return false;
        return true;
    }

    private static void SmokeTest(InstallerLog log, string exePath)
    {
        // `waterfree doctor` verifies every required Python dependency is
        // actually bundled (networkx, tree-sitter grammars, providers, ...).
        // It both confirms the exe starts and catches a degraded runtime that
        // would otherwise "install fine" but fail to function. Exit 0 = healthy.
        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = "doctor",
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            CreateNoWindow = true
        };

        using var proc = new Process { StartInfo = psi };
        proc.Start();
        var stdout = proc.StandardOutput.ReadToEnd();
        var stderr = proc.StandardError.ReadToEnd();
        proc.WaitForExit(60000);

        if (!proc.HasExited)
        {
            try { proc.Kill(true); } catch { /* ignore */ }
            throw new InvalidOperationException("Smoke test: waterfree exe did not return within 60s.");
        }

        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Smoke test failed: `waterfree doctor` exit={proc.ExitCode}. " +
                $"The runtime is missing required dependencies.\n{stdout}\n{stderr}");
        }

        log.Info($"Smoke test OK: waterfree runtime dependencies verified.\n{stdout}");
    }

    private static void InstallVsixIfPossible(InstallerLog log, string? vsixPath)
    {
        if (string.IsNullOrWhiteSpace(vsixPath))
        {
            log.Info("VSIX path not provided; skipping VS Code install.");
            return;
        }

        vsixPath = Path.GetFullPath(vsixPath);
        if (!File.Exists(vsixPath))
        {
            log.Info($"VSIX not found; skipping VS Code install: {vsixPath}");
            return;
        }

        var codeCmd = FindVsCodeCommand();
        if (string.IsNullOrWhiteSpace(codeCmd))
        {
            log.Info("VS Code CLI not found; skipping VSIX install.");
            return;
        }

        log.Info($"Installing VSIX via: {codeCmd}");

        // .cmd files cannot be launched directly with UseShellExecute=false;
        // they must be invoked through cmd.exe.
        ProcessStartInfo psi;
        if (codeCmd.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase))
        {
            psi = new ProcessStartInfo
            {
                FileName = "cmd.exe",
                // cmd.exe /s /c "<command>": with /s, cmd strips exactly the
                // outermost pair of quotes and runs the remainder verbatim, so a
                // code.cmd path containing spaces ("Microsoft VS Code") survives.
                // Without the extra wrapping quotes cmd strips the quotes around
                // the path itself and fails with:
                //   '...\Local\Programs\Microsoft' is not recognized ...
                Arguments = $"/s /c \"\"{codeCmd}\" --install-extension \"{vsixPath}\" --force\"",
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                CreateNoWindow = true
            };
        }
        else
        {
            psi = new ProcessStartInfo
            {
                FileName = codeCmd,
                Arguments = $"--install-extension \"{vsixPath}\" --force",
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                CreateNoWindow = true
            };
        }

        using var proc = new Process { StartInfo = psi };
        proc.Start();
        var stdout = proc.StandardOutput.ReadToEnd();
        var stderr = proc.StandardError.ReadToEnd();
        proc.WaitForExit();

        if (proc.ExitCode != 0)
        {
            log.Info($"VS Code extension install failed (non-fatal): exit={proc.ExitCode} stdout={stdout} stderr={stderr}");
            return;
        }

        log.Info($"VS Code extension installed. stdout={stdout}");
    }

    private static string? FindVsCodeCommand()
    {
        var candidates = new List<string>();

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        var programFilesX86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);

        // Standard VS Code (user install — most common on Windows)
        candidates.Add(Path.Combine(localAppData, "Programs", "Microsoft VS Code", "bin", "code.cmd"));
        // System-wide installs
        candidates.Add(Path.Combine(programFiles, "Microsoft VS Code", "bin", "code.cmd"));
        candidates.Add(Path.Combine(programFilesX86, "Microsoft VS Code", "bin", "code.cmd"));
        // VS Code Insiders
        candidates.Add(Path.Combine(localAppData, "Programs", "Microsoft VS Code Insiders", "bin", "code-insiders.cmd"));
        candidates.Add(Path.Combine(programFiles, "Microsoft VS Code Insiders", "bin", "code-insiders.cmd"));

        // Fall back to PATH — deferred custom actions may have a restricted PATH,
        // so try the hard-coded locations above first.
        var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var segment in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(segment)) continue;
            var dir = segment.Trim();
            candidates.Add(Path.Combine(dir, "code.cmd"));
            candidates.Add(Path.Combine(dir, "code.exe"));
            candidates.Add(Path.Combine(dir, "code-insiders.cmd"));
        }

        foreach (var candidate in candidates)
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static string EscapeToml(string value)
    {
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    private static string GetUserHome()
    {
        return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    }

    private static bool HasArg(string[] args, string name)
    {
        return args.Any(a => string.Equals(a, name, StringComparison.OrdinalIgnoreCase));
    }

    private static string? GetArgValue(string[] args, string name)
    {
        for (var i = 0; i < args.Length - 1; i++)
        {
            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase))
            {
                return args[i + 1];
            }
        }
        return null;
    }
}

internal sealed class InstallerLog : IDisposable
{
    private readonly string _logPath;
    private readonly StreamWriter _writer;

    public string LogPath => _logPath;

    public InstallerLog(string installDir, string? logPathOverride)
    {
        var logDir = Path.Combine(installDir, "logs", "installer");
        var stamp = DateTime.UtcNow.ToString("yyyyMMdd-HHmmss");
        var defaultLogPath = Path.Combine(logDir, $"installer-{stamp}.log");

        _logPath = !string.IsNullOrWhiteSpace(logPathOverride)
            ? Path.GetFullPath(logPathOverride)
            : defaultLogPath;

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
        }
        catch
        {
            Directory.CreateDirectory(logDir);
            _logPath = defaultLogPath;
        }

        _writer = new StreamWriter(_logPath, append: true, encoding: new UTF8Encoding(false));
        _writer.AutoFlush = true;
        Info($"Log file: {_logPath}");
    }

    public void Info(string message) => Write("INFO", message);

    public void Error(string message) => Write("ERROR", message);

    private void Write(string level, string message)
    {
        var line = $"{DateTime.UtcNow:O} [{level}] {message}";
        _writer.WriteLine(line);
        Console.WriteLine(line);
    }

    public void Dispose()
    {
        _writer.Dispose();
    }
}
