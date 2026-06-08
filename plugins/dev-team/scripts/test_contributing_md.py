"""Tests that CONTRIBUTING.md exists and has the required sections.

Repro test for Issue-10: agents in the dev-team pipeline read CONTRIBUTING.md
for project guidelines. The file does not exist, so agents proceed without any
coding standards, resulting in inconsistent or guideline-free output.
"""

from pathlib import Path

# Three levels up from scripts/ → plugins/dev-team → plugins → repo root
REPO_ROOT = Path(__file__).parents[3]
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

REQUIRED_SECTIONS = [
    "## How to Contribute",
    "## Code Guidelines",
    "## Code of Conduct",
]


class TestContributingMd:
    def test_contributing_md_exists(self):
        assert CONTRIBUTING.exists(), (
            f"CONTRIBUTING.md not found at {CONTRIBUTING}. "
            "Developer and Reviewer agents cannot load project guidelines."
        )

    def test_contributing_md_has_required_sections(self):
        assert CONTRIBUTING.exists(), "CONTRIBUTING.md missing — cannot check sections"
        content = CONTRIBUTING.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            assert section in content, f"CONTRIBUTING.md is missing section: {section}"

    def test_contributing_md_has_no_csharp_patterns(self):
        """Ensure no C#-specific guidance leaked in from AdaptiveRemote source."""
        assert CONTRIBUTING.exists(), "CONTRIBUTING.md missing — cannot check content"
        content = CONTRIBUTING.read_text(encoding="utf-8")
        forbidden = ["MSTest", "NuGet", "LoggerMessage", "TaskCompletionSource", "WaitHelper"]
        found = [term for term in forbidden if term in content]
        assert not found, f"C#-specific patterns found in CONTRIBUTING.md: {found}"
