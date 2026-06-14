// WaterFree launcher — a tiny native shim that runs the Python backend.
//
// Why this exists
// ---------------
// The PyInstaller "freeze everything" route bundled torch + transformers +
// scipy into a 4.4 GB one-dir runtime — far too large to embed in an MSI CAB.
// This launcher ships instead: a few-KB exe plus the pure-Python `backend/`
// source tree. The heavy dependencies live in a shared virtual environment on
// the machine; the launcher just shells into that interpreter.
//
// Contract (kept identical to the old frozen exe, so the VS Code extension and
// the `waterfree <area> <action>` CLI surface don't change):
//
//     waterfree serve                  -> python -m backend.main serve
//     waterfree doctor                 -> python -m backend.main doctor
//     waterfree <area> <action> ...    -> python -m backend.main <area> <action> ...
//
// Python resolution (dev/internal build):
//   1. $WATERFREE_PYTHON, if it points at an existing file
//   2. C:\Projects\.local\Scripts\python.exe (the shared dev venv)
//   3. python.exe / python3.exe on PATH
//
// Backend resolution:
//   1. $WATERFREE_BACKEND_ROOT (a dir containing `backend\main.py`) — lets a
//      developer point the installed launcher at a live repo checkout
//   2. the launcher's own directory (the runtime ships `backend\` next to the exe)
//
// stdio is inherited (not redirected): `serve` speaks newline-delimited JSON-RPC
// over stdin/stdout, and CLI subcommands print straight to the caller's console.

using System.Diagnostics;

namespace WaterFreeLauncher;

internal static class Program
{
    // Shared dev/internal interpreter. Override with WATERFREE_PYTHON.
    private const string DefaultPython = @"C:\Projects\.local\Scripts\python.exe";

    private static int Main(string[] args)
    {
        var python = ResolvePython();
        if (python is null)
        {
            Console.Error.WriteLine(
                "waterfree: no Python interpreter found.\n" +
                "  Set WATERFREE_PYTHON to a python.exe, or create the shared venv at\n" +
                $"  {DefaultPython}");
            return 1;
        }

        var backendRoot = ResolveBackendRoot();
        if (backendRoot is null)
        {
            Console.Error.WriteLine(
                "waterfree: could not locate the 'backend' package.\n" +
                "  Expected backend\\main.py next to this exe, or set\n" +
                "  WATERFREE_BACKEND_ROOT to a directory that contains backend\\main.py.");
            return 1;
        }

        var psi = new ProcessStartInfo
        {
            FileName = python,
            // Inherit the parent's console handles so `serve` can speak JSON-RPC
            // over stdin/stdout and CLI output flows straight through.
            UseShellExecute = false,
        };
        psi.ArgumentList.Add("-m");
        psi.ArgumentList.Add("backend.main");
        foreach (var a in args)
        {
            psi.ArgumentList.Add(a);
        }

        // Make `backend` importable regardless of the caller's working directory.
        // We deliberately leave the child's cwd alone: CLI subcommands resolve the
        // target workspace from the current directory, so it must stay the user's.
        var existing = Environment.GetEnvironmentVariable("PYTHONPATH");
        psi.Environment["PYTHONPATH"] = string.IsNullOrEmpty(existing)
            ? backendRoot
            : backendRoot + Path.PathSeparator + existing;
        // Don't litter the install tree / repo with __pycache__ on every run.
        psi.Environment["PYTHONDONTWRITEBYTECODE"] = "1";

        try
        {
            using var proc = Process.Start(psi);
            if (proc is null)
            {
                Console.Error.WriteLine($"waterfree: failed to start interpreter: {python}");
                return 1;
            }
            proc.WaitForExit();
            return proc.ExitCode;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"waterfree: failed to launch backend via {python}: {ex.Message}");
            return 1;
        }
    }

    private static string? ResolvePython()
    {
        var fromEnv = Environment.GetEnvironmentVariable("WATERFREE_PYTHON");
        if (!string.IsNullOrWhiteSpace(fromEnv) && File.Exists(fromEnv))
        {
            return Path.GetFullPath(fromEnv);
        }
        if (File.Exists(DefaultPython))
        {
            return DefaultPython;
        }
        return FindOnPath("python.exe") ?? FindOnPath("python3.exe");
    }

    private static string? ResolveBackendRoot()
    {
        var fromEnv = Environment.GetEnvironmentVariable("WATERFREE_BACKEND_ROOT");
        if (!string.IsNullOrWhiteSpace(fromEnv) &&
            File.Exists(Path.Combine(fromEnv, "backend", "main.py")))
        {
            return Path.GetFullPath(fromEnv);
        }

        // Walk up from the launcher's own directory looking for backend\main.py.
        // Installed layout has it right next to the exe; a repo checkout has it a
        // couple of levels up.
        var dir = AppContext.BaseDirectory;
        for (var i = 0; i < 6 && !string.IsNullOrEmpty(dir); i++)
        {
            if (File.Exists(Path.Combine(dir, "backend", "main.py")))
            {
                return Path.GetFullPath(dir);
            }
            dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        }
        return null;
    }

    private static string? FindOnPath(string exe)
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (var segment in path.Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(segment))
            {
                continue;
            }
            try
            {
                var candidate = Path.Combine(segment.Trim(), exe);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch
            {
                // Ignore malformed PATH segments.
            }
        }
        return null;
    }
}
