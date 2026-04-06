using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace WaterFreeInstallerHelper;

internal static class Program
{
    private sealed record ServerSpec(string Name, string Mode);

    private static readonly ServerSpec[] Servers =
    {
        new("waterfree-index", "index"),
        new("waterfree-knowledge", "knowledge"),
        new("waterfree-todos", "todos"),
        new("waterfree-debug", "debug"),
        new("waterfree-testing", "testing"),
        new("waterfree-qa-summary", "qa-summary")
    };

    private static readonly string[] LegacyServers =
    {
        "pairprogram-debug",
        "pairprogram-index",
        "pairprogram-knowledge",
        "pairprogram-todos"
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
            var exePath = Path.Combine(installDir, "bin", "waterfree-mcp.exe");
            if (!File.Exists(exePath))
            {
                log.Error($"Executable not found: {exePath}");
                return 3;
            }

            if (uninstall)
            {
                RemoveCodexConfig(log);
                RemoveClaudeConfig(log);
                log.Info("Uninstall cleanup complete.");
                return 0;
            }

            UpsertCodexConfig(log, exePath);
            UpsertClaudeConfig(log, exePath);
            InstallVsixIfPossible(log, vsixPath);
            SmokeTest(log, exePath);

            log.Info("Install helper completed successfully.");
            return 0;
        }
        catch (Exception ex)
        {
            log.Error($"Installer helper failed: {ex}");
            return 1;
        }
        finally
        {
            log.Dispose();
        }
    }

    private static void UpsertCodexConfig(InstallerLog log, string exePath)
    {
        var codexDir = Path.Combine(GetUserHome(), ".codex");
        var configPath = Path.Combine(codexDir, "config.toml");
        Directory.CreateDirectory(codexDir);

        var lines = File.Exists(configPath)
            ? File.ReadAllLines(configPath).ToList()
            : new List<string>();

        var removeNames = Servers.Select(s => s.Name).Concat(LegacyServers).ToHashSet(StringComparer.OrdinalIgnoreCase);
        lines = RemoveTomlSections(lines, removeNames);

        if (lines.Count > 0 && !string.IsNullOrWhiteSpace(lines[^1]))
        {
            lines.Add(string.Empty);
        }

        foreach (var server in Servers)
        {
            lines.Add($"[mcp_servers.{server.Name}]");
            lines.Add($"command = \"{EscapeToml(exePath)}\"");
            lines.Add($"args = [\"mcp\", \"{server.Mode}\"]");
            lines.Add(string.Empty);
        }

        File.WriteAllLines(configPath, lines, new UTF8Encoding(false));
        log.Info($"Codex MCP config updated: {configPath}");
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

        var removeNames = Servers.Select(s => s.Name).Concat(LegacyServers).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var lines = File.ReadAllLines(configPath).ToList();
        var updated = RemoveTomlSections(lines, removeNames);
        File.WriteAllLines(configPath, updated, new UTF8Encoding(false));
        log.Info($"Codex MCP config cleaned: {configPath}");
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

    private static void UpsertClaudeConfig(InstallerLog log, string exePath)
    {
        var configPath = Path.Combine(GetUserHome(), ".claude.json");
        JsonNode rootNode;
        var originalWasArray = false;

        if (File.Exists(configPath))
        {
            var text = File.ReadAllText(configPath);
            rootNode = JsonNode.Parse(text) ?? new JsonObject();
        }
        else
        {
            rootNode = new JsonObject();
        }

        if (rootNode is not JsonObject root)
        {
            root = new JsonObject();
        }

        var mcpServersNode = root["mcpServers"];
        Dictionary<string, JsonObject> serverMap = new(StringComparer.OrdinalIgnoreCase);

        if (mcpServersNode is JsonArray array)
        {
            originalWasArray = true;
            foreach (var item in array)
            {
                if (item is not JsonObject obj) continue;
                var name = obj["name"]?.GetValue<string>();
                if (string.IsNullOrWhiteSpace(name)) continue;
                serverMap[name] = obj;
            }
        }
        else if (mcpServersNode is JsonObject obj)
        {
            foreach (var kvp in obj)
            {
                if (kvp.Value is JsonObject entry)
                {
                    serverMap[kvp.Key] = entry;
                }
            }
        }

        foreach (var legacy in LegacyServers)
        {
            serverMap.Remove(legacy);
        }

        foreach (var server in Servers)
        {
            var entry = new JsonObject
            {
                ["command"] = exePath,
                ["args"] = new JsonArray("mcp", server.Mode)
            };
            serverMap[server.Name] = entry;
        }

        if (originalWasArray)
        {
            var newArray = new JsonArray();
            foreach (var kvp in serverMap.OrderBy(k => k.Key))
            {
                var entry = new JsonObject
                {
                    ["name"] = kvp.Key,
                    ["command"] = kvp.Value["command"],
                    ["args"] = kvp.Value["args"]
                };
                newArray.Add(entry);
            }
            root["mcpServers"] = newArray;
        }
        else
        {
            var newObj = new JsonObject();
            foreach (var kvp in serverMap.OrderBy(k => k.Key))
            {
                newObj[kvp.Key] = kvp.Value;
            }
            root["mcpServers"] = newObj;
        }

        var options = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(configPath, root.ToJsonString(options), new UTF8Encoding(false));
        log.Info($"Claude MCP config updated: {configPath}");
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
            foreach (var name in Servers.Select(s => s.Name).Concat(LegacyServers).ToList())
            {
                obj.Remove(name);
            }
            root["mcpServers"] = obj;
        }

        var options = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(configPath, root.ToJsonString(options), new UTF8Encoding(false));
        log.Info($"Claude MCP config cleaned: {configPath}");
    }

    private static bool ShouldKeepServer(string name)
    {
        if (string.IsNullOrWhiteSpace(name)) return true;
        if (LegacyServers.Contains(name, StringComparer.OrdinalIgnoreCase)) return false;
        if (Servers.Any(s => s.Name.Equals(name, StringComparison.OrdinalIgnoreCase))) return false;
        return true;
    }

    private static void SmokeTest(InstallerLog log, string exePath)
    {
        foreach (var server in Servers)
        {
            var psi = new ProcessStartInfo
            {
                FileName = exePath,
                Arguments = $"mcp {server.Mode}",
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                CreateNoWindow = true
            };

            using var proc = new Process { StartInfo = psi };
            proc.Start();
            Thread.Sleep(TimeSpan.FromSeconds(2));

            if (proc.HasExited)
            {
                var stderr = proc.StandardError.ReadToEnd();
                var stdout = proc.StandardOutput.ReadToEnd();
                throw new InvalidOperationException(
                    $"MCP mode '{server.Mode}' exited early. stdout={stdout} stderr={stderr}");
            }

            try
            {
                proc.Kill(true);
                proc.WaitForExit(2000);
            }
            catch
            {
                // Ignore termination failures in installer context.
            }

            log.Info($"Smoke test OK: {server.Mode}");
        }
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

        var psi = new ProcessStartInfo
        {
            FileName = codeCmd,
            Arguments = $"--install-extension \"{vsixPath}\" --force",
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            CreateNoWindow = true
        };

        using var proc = new Process { StartInfo = psi };
        proc.Start();
        proc.WaitForExit();

        var stdout = proc.StandardOutput.ReadToEnd();
        var stderr = proc.StandardError.ReadToEnd();
        if (proc.ExitCode != 0)
        {
            log.Info($"VS Code extension install failed (non-fatal): exit={proc.ExitCode} stdout={stdout} stderr={stderr}");
            return;
        }

        log.Info("VS Code extension installed.");
    }

    private static string? FindVsCodeCommand()
    {
        var candidates = new List<string>();

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);

        candidates.Add(Path.Combine(localAppData, "Programs", "Microsoft VS Code", "bin", "code.cmd"));
        candidates.Add(Path.Combine(programFiles, "Microsoft VS Code", "bin", "code.cmd"));

        var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var segment in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(segment)) continue;
            candidates.Add(Path.Combine(segment.Trim(), "code.cmd"));
            candidates.Add(Path.Combine(segment.Trim(), "code.exe"));
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
