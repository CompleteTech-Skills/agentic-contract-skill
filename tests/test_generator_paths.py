"""Exercise generator path safety with synthetic files and real CLI runs."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


GENERATOR = Path(__file__).resolve().parents[1] / "generate_contract.py"
CONFIG = """[provider]
legal_name = Synthetic Audit Provider
[client]
legal_name = Synthetic Audit Client
[agreement]
title = Synthetic Path Audit
contract_id = PATH-AUDIT-001
[branding]
cover_page_enabled = no
letterhead_enabled = no
header_enabled = no
footer_enabled = no
watermark_enabled = no
envelope_enabled = yes
brand_name = Synthetic Audit
logo_monogram = SA
[envelope]
include_return_address = no
"""
TEMPLATE = "# Synthetic Path Audit\n\nContract marker: {{ agreement.contract_id }}\n"


class GeneratorPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="contract-path-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, name: str) -> dict[str, Path]:
        root = self.root / name
        root.mkdir()
        paths = {
            "root": root,
            "config": root / "input.ini",
            "override": root / "override.ini",
            "template": root / "template.md",
            "pdf": root / "generated" / "contract.pdf",
            "markdown": root / "generated" / "contract.md",
            "envelope": root / "generated" / "envelope.pdf",
        }
        paths["config"].write_text(CONFIG, encoding="utf-8")
        paths["override"].write_text("[agreement]\ncontract_id = PATH-AUDIT-OVERRIDE\n", encoding="utf-8")
        paths["template"].write_text(TEMPLATE, encoding="utf-8")
        return paths

    def snapshot(self, root: Path) -> dict[str, bytes | str]:
        result: dict[str, bytes | str] = {}
        for path in root.rglob("*"):
            name = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[name] = f"symlink:{os.readlink(path)}"
            elif path.is_file():
                result[name] = path.read_bytes()
            else:
                result[name] = "directory"
        return result

    def run_generator(
        self, paths: dict[str, Path], *, default_markdown: bool = False,
        default_envelope: bool = False, no_envelope: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable, str(GENERATOR),
            "--config", str(paths["config"]), str(paths["override"]),
            "--template", str(paths["template"]), "--out", str(paths["pdf"]),
        ]
        if not default_markdown:
            command.extend(["--markdown-out", str(paths["markdown"])])
        if not default_envelope:
            command.extend(["--envelope-out", str(paths["envelope"])])
        if no_envelope:
            command.append("--no-envelope")
        return subprocess.run(command, cwd=paths["root"], capture_output=True, text=True, timeout=20)

    def assert_rejected(self, paths: dict[str, Path], **options: bool) -> None:
        before = self.snapshot(paths["root"])
        completed = self.run_generator(paths, **options)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertRegex(completed.stderr, r"output (aliases|would overwrite input file)")
        self.assertEqual(self.snapshot(paths["root"]), before, "Rejected run changed files or created directories")

    def assert_generated(self, paths: dict[str, Path], **options: bool) -> None:
        inputs = {name: paths[name].read_bytes() for name in ("config", "override", "template")}
        completed = self.run_generator(paths, **options)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        pdf = paths["pdf"].read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(re.findall(rb"/MediaBox\s*\[([^]]+)\]", pdf), [b" 0 0 612 792 "])
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("Contract marker: PATH-AUDIT-OVERRIDE", markdown)
        self.assertNotIn("{{", markdown)
        for name, original in inputs.items():
            self.assertEqual(paths[name].read_bytes(), original)

    def test_rejects_each_pair_of_outputs_before_writing(self) -> None:
        for first, second in (("pdf", "markdown"), ("pdf", "envelope"), ("markdown", "envelope")):
            with self.subTest(first=first, second=second):
                paths = self.fixture(f"{first}-{second}")
                paths[second] = paths[first]
                self.assert_rejected(paths)

    def test_rejects_colliding_default_outputs_before_writing(self) -> None:
        paths = self.fixture("default-markdown")
        paths["pdf"] = paths["markdown"]
        self.assert_rejected(paths, default_markdown=True)
        paths = self.fixture("default-envelope")
        paths["pdf"] = paths["envelope"].with_name("addressed_envelope.pdf")
        self.assert_rejected(paths, default_envelope=True)

    def test_rejects_each_output_aliasing_any_config_or_template(self) -> None:
        for output in ("pdf", "markdown", "envelope"):
            for source in ("config", "override", "template"):
                with self.subTest(output=output, source=source):
                    paths = self.fixture(f"{output}-{source}")
                    paths[output] = paths[source]
                    self.assert_rejected(paths)

    def test_rejects_normalized_path_aliases(self) -> None:
        paths = self.fixture("normalized")
        paths["markdown"] = paths["pdf"].parent / "subdirectory" / ".." / paths["pdf"].name
        self.assert_rejected(paths)

    def check_link_aliases(self, symbolic: bool) -> None:
        for output, source in (("markdown", "pdf"), ("pdf", "config"), ("markdown", "template"), ("envelope", "override")):
            with self.subTest(output=output, source=source, symbolic=symbolic):
                paths = self.fixture(f"link-{output}-{source}")
                paths[output].parent.mkdir(parents=True, exist_ok=True)
                if source == "pdf":
                    paths[source].write_bytes(b"SYNTHETIC PREVIOUS OUTPUT")
                try:
                    if symbolic:
                        paths[output].symlink_to(paths[source])
                    else:
                        os.link(paths[source], paths[output])
                except (NotImplementedError, OSError) as error:
                    self.skipTest(f"{'Symbolic' if symbolic else 'Hard'} links unavailable: {error}")
                self.assert_rejected(paths)

    def test_rejects_symbolic_link_aliases(self) -> None:
        self.check_link_aliases(symbolic=True)

    def test_rejects_hard_link_aliases(self) -> None:
        self.check_link_aliases(symbolic=False)

    def test_distinct_outputs_still_replace_previous_artifacts(self) -> None:
        paths = self.fixture("replace-outputs")
        paths["pdf"].parent.mkdir()
        for output in ("pdf", "markdown", "envelope"):
            paths[output].write_bytes(b"SYNTHETIC PREVIOUS OUTPUT")
        self.assert_generated(paths)
        envelope = paths["envelope"].read_bytes()
        self.assertTrue(envelope.startswith(b"%PDF-"))
        self.assertEqual(re.findall(rb"/MediaBox\s*\[([^]]+)\]", envelope), [b" 0 0 684 297 "])

    def test_distinct_default_outputs_still_work(self) -> None:
        paths = self.fixture("default-outputs")
        paths["envelope"] = paths["pdf"].with_name("addressed_envelope.pdf")
        self.assert_generated(paths, default_markdown=True, default_envelope=True)
        self.assertTrue(paths["envelope"].read_bytes().startswith(b"%PDF-"))

    def test_no_envelope_flag_ignores_colliding_envelope_path(self) -> None:
        paths = self.fixture("flag-disables-envelope")
        paths["envelope"] = paths["config"]
        self.assert_generated(paths, no_envelope=True)

    def test_config_disabled_envelope_ignores_colliding_envelope_path(self) -> None:
        paths = self.fixture("config-disables-envelope")
        paths["config"].write_text(CONFIG.replace("envelope_enabled = yes", "envelope_enabled = no"), encoding="utf-8")
        paths["envelope"] = paths["template"]
        self.assert_generated(paths)


if __name__ == "__main__":
    unittest.main()
