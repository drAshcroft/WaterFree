"""
Tests for the .NET runner.

Exists because of the knowledge-base entry "Waterfree testing may return 0/0
for .NET solutions": the generic runner exited 0 having discovered nothing, and
a zero exit code was read as passing tests.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.testing.dotnet import (
    find_solution_or_projects,
    find_test_projects,
    is_dotnet_project,
    parse_trx,
    scan_test_names,
)

TRX = """<?xml version="1.0" encoding="UTF-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <Results>
    <UnitTestResult testName="Server.Tests.LobbyTests.JoinsAnOpenLobby"
                    outcome="Passed" duration="00:00:00.1234567" />
    <UnitTestResult testName="Server.Tests.LobbyTests.RejectsAFullLobby"
                    outcome="Failed" duration="00:00:01.5000000">
      <Output>
        <ErrorInfo>
          <Message>Assert.Equal() Failure</Message>
          <StackTrace>at Server.Tests.LobbyTests.RejectsAFullLobby()</StackTrace>
        </ErrorInfo>
      </Output>
    </UnitTestResult>
    <UnitTestResult testName="Server.Tests.LobbyTests.PendingFeature"
                    outcome="NotExecuted" />
  </Results>
</TestRun>
"""

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.9.0" />
    <PackageReference Include="xunit" Version="2.7.0" />
  </ItemGroup>
</Project>
"""

LIBRARY_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
</Project>
"""


class TrxParsingTests(unittest.TestCase):
    def test_counts_pass_fail_and_skip(self) -> None:
        results = parse_trx(TRX)
        self.assertEqual(len(results), 3)
        self.assertEqual(sum(1 for r in results if r.passed), 2)  # pass + skip
        self.assertEqual(sum(1 for r in results if not r.passed), 1)

    def test_keeps_the_failure_message_and_stack(self) -> None:
        failed = next(r for r in parse_trx(TRX) if not r.passed)
        self.assertIn("Assert.Equal() Failure", failed.error)
        self.assertIn("RejectsAFullLobby", failed.error)

    def test_converts_trx_duration_to_milliseconds(self) -> None:
        results = {r.name: r for r in parse_trx(TRX)}
        self.assertAlmostEqual(
            results["Server.Tests.LobbyTests.RejectsAFullLobby"].duration_ms,
            1500.0,
            places=3,
        )

    def test_skip_is_visible_but_not_a_failure(self) -> None:
        skipped = next(r for r in parse_trx(TRX) if r.name.endswith("PendingFeature"))
        self.assertTrue(skipped.passed)
        self.assertEqual(skipped.error, "skipped")

    def test_unknown_outcome_is_treated_as_a_failure(self) -> None:
        """An outcome we do not recognise is not evidence of a pass."""
        xml = TRX.replace('outcome="Passed"', 'outcome="Aborted"')
        aborted = next(r for r in parse_trx(xml) if r.name.endswith("JoinsAnOpenLobby"))
        self.assertFalse(aborted.passed)

    def test_malformed_xml_yields_nothing_rather_than_raising(self) -> None:
        self.assertEqual(parse_trx("not xml at all"), [])


class DiscoveryTests(unittest.TestCase):
    def test_prefers_a_solution_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.sln").write_text("", encoding="utf-8")
            (root / "Server.Tests").mkdir()
            (root / "Server.Tests" / "Server.Tests.csproj").write_text(CSPROJ, encoding="utf-8")
            targets = find_solution_or_projects(tmp)
            self.assertEqual([p.name for p in targets], ["App.sln"])

    def test_falls_back_to_test_projects(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Server.Tests").mkdir()
            (root / "Server.Tests" / "Server.Tests.csproj").write_text(CSPROJ, encoding="utf-8")
            self.assertEqual(
                [p.name for p in find_solution_or_projects(tmp)],
                ["Server.Tests.csproj"],
            )

    def test_plain_library_is_not_a_test_project(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Core").mkdir()
            (root / "Core" / "Core.csproj").write_text(LIBRARY_CSPROJ, encoding="utf-8")
            self.assertEqual(find_test_projects(tmp), [])
            self.assertFalse(is_dotnet_project(tmp))


class SourceScanTests(unittest.TestCase):
    SOURCE = """
namespace Server.Tests;

public class LobbyTests
{
    [Fact]
    public void JoinsAnOpenLobby()
    {
    }

    [Theory]
    [InlineData(1)]
    [InlineData(2)]
    public async Task RejectsAFullLobby(int seats)
    {
    }

    public void NotATest()
    {
    }
}
"""

    def test_lists_attributed_methods_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Server.Tests"
            project.mkdir()
            (project / "Server.Tests.csproj").write_text(CSPROJ, encoding="utf-8")
            (project / "LobbyTests.cs").write_text(self.SOURCE, encoding="utf-8")
            names = scan_test_names(tmp)

        self.assertIn("Server.Tests.LobbyTests.JoinsAnOpenLobby", names)
        self.assertIn("Server.Tests.LobbyTests.RejectsAFullLobby", names)
        self.assertNotIn("Server.Tests.LobbyTests.NotATest", names)


if __name__ == "__main__":
    unittest.main()
