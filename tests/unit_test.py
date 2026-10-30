"""Offline unit tests for the domain layer.

No network, no ComfyUI, no writes outside a temp directory: every test patches
the module-level paths it needs, so running them never touches project/.
    python -m unittest discover -s tests -p "unit_test.py"
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import comfy  # noqa: E402
import ollama  # noqa: E402
import remediation  # noqa: E402
import studio  # noqa: E402


@contextmanager
def patched(**attrs):
    """Temporarily replace module attributes (paths, or comfy_root in tests)."""
    saved = {key: getattr(studio, key) for key in attrs}
    for key, value in attrs.items():
        setattr(studio, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(studio, key, value)


class TempProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.art = self.root / "project"
        self.art.mkdir()
        self.comfy = self.root / "ComfyUI"

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, rel: str, data: bytes = b"image-bytes") -> Path:
        path = self.art / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


# --------------------------------------------------------------------------- #
# options / generation
# --------------------------------------------------------------------------- #
class TestNormalizeOptions(unittest.TestCase):
    def test_clamps_and_rounds_to_eight(self):
        opts = comfy.normalize_options({"width": 1000, "height": 300, "steps": 500,
                                        "cfg": 99, "batch": 9})
        self.assertEqual(opts["width"], 1000)
        self.assertEqual(opts["height"], 296)
        self.assertEqual(opts["steps"], 120)
        self.assertEqual(opts["cfg"], 20.0)
        self.assertEqual(opts["batch_size"], 4)

    def test_low_values_are_clamped_up(self):
        opts = comfy.normalize_options({"width": 1, "steps": 0, "cfg": 0.1, "batch": 0})
        self.assertEqual(opts["width"], 256)
        self.assertEqual(opts["steps"], 1)
        self.assertEqual(opts["cfg"], 1.0)
        self.assertEqual(opts["batch_size"], 1)

    def test_seed_random_when_missing_or_negative(self):
        for raw in ({"seed": -1}, {"seed": "-1"}, {}):
            seed = comfy.normalize_options(raw)["seed"]
            self.assertIsInstance(seed, int)
            self.assertGreaterEqual(seed, 0)
            self.assertLessEqual(seed, comfy.SEED_MAX)

    def test_only_supplied_keys_reach_the_workflow(self):
        overrides = comfy.normalize_options({})["overrides"]
        self.assertEqual(set(overrides), {"seed"})


class TestPrepareGeneration(unittest.TestCase):
    def test_prompt_override_reaches_the_graph(self):
        prepared = comfy.prepare_generation(
            "stage1_master", {"hair": "ignored"}, {"width": 512},
            prompt={"positive": "HELLO", "negative": "NEG"})
        graph = prepared["graph"]
        self.assertEqual(prepared["prompt"]["positive"], "HELLO")
        self.assertEqual(graph["5"]["inputs"]["text"], "HELLO")
        self.assertEqual(graph["6"]["inputs"]["text"], "NEG")
        self.assertEqual(graph["4"]["inputs"]["width"], 512)

    def test_template_render_is_used_without_override(self):
        prepared = comfy.prepare_generation("stage1_master",
                                            {"hair": "long blue hair"})
        self.assertIn("long blue hair", prepared["prompt"]["positive"])

    def test_unknown_workflow_is_reported(self):
        with self.assertRaises(Exception):
            comfy.prepare_generation("stage1_master", {}, {}, "missing-workflow.json")


# --------------------------------------------------------------------------- #
# urls / path guards
# --------------------------------------------------------------------------- #
class TestMediaUrl(unittest.TestCase):
    def test_project_url_encodes_relative_path(self):
        url = studio.media_url({"source": "final", "rel": "03_illust/final/a b.png"})
        self.assertTrue(url.startswith("/api/image?source=project&rel="))
        self.assertIn("a+b.png", url)  # query encoding turns spaces into +

    def test_comfy_url_carries_subfolder_and_type(self):
        url = studio.media_url({"source": "comfy", "name": "x.png",
                                "subfolder": "anima", "type": "output"})
        self.assertIn("filename=x.png", url)
        self.assertIn("subfolder=anima", url)
        self.assertIn("type=output", url)


class TestResolveProjectMedia(TempProject):
    def test_accepts_file_inside_project(self):
        self.make("03_illust/final/a.png")
        with patched(ART_DIR=self.art):
            self.assertIsNotNone(studio.resolve_project_media("03_illust/final/a.png"))

    def test_rejects_traversal(self):
        (self.root / "secret.png").write_bytes(b"x")
        with patched(ART_DIR=self.art):
            self.assertIsNone(studio.resolve_project_media("../secret.png"))

    def test_rejects_unsupported_suffix(self):
        self.make("03_illust/final/notes.txt")
        with patched(ART_DIR=self.art):
            self.assertIsNone(studio.resolve_project_media("03_illust/final/notes.txt"))


class TestResolveComfyMedia(TempProject):
    def test_accepts_file_under_output(self):
        target = self.comfy / "output" / "anima" / "x.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        with patched(comfy_root=lambda: self.comfy):
            self.assertIsNotNone(studio.resolve_comfy_media("x.png", "anima"))

    def test_rejects_escape_and_missing(self):
        (self.comfy / "output").mkdir(parents=True)
        (self.root / "secret.png").write_bytes(b"x")
        with patched(comfy_root=lambda: self.comfy):
            self.assertIsNone(studio.resolve_comfy_media("../../secret.png", "anima"))
            self.assertIsNone(studio.resolve_comfy_media("x.png", "../../.."))
            self.assertIsNone(studio.resolve_comfy_media("missing.png", "anima"))

    def test_rejects_when_comfy_root_unset(self):
        with patched(comfy_root=lambda: Path("")):
            self.assertIsNone(studio.resolve_comfy_media("x.png", ""))


# --------------------------------------------------------------------------- #
# gallery
# --------------------------------------------------------------------------- #
class TestGallery(TempProject):
    def test_empty_roots_report_missing(self):
        with patched(ART_DIR=self.art, comfy_root=lambda: self.comfy):
            snapshot = studio.gallery_snapshot()
        self.assertEqual(snapshot["total"], 0)
        self.assertTrue(all(not item["exists"] for item in snapshot["scanned"]))

    def test_collects_project_and_comfy_output(self):
        self.make("03_illust/final/a.png")
        out = self.comfy / "output" / "anima"
        out.mkdir(parents=True)
        (out / "b.png").write_bytes(b"x")
        with patched(ART_DIR=self.art, comfy_root=lambda: self.comfy):
            snapshot = studio.gallery_snapshot()
        sources = sorted({item["source"] for item in snapshot["items"]})
        self.assertEqual(sources, ["comfy", "final"])
        urls = [studio.media_url(item) for item in snapshot["items"]]
        self.assertTrue(any(url.startswith("/api/image?source=comfy") for url in urls))
        self.assertTrue(any(url.startswith("/api/image?source=project") for url in urls))


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #
class TestArchiveIllust(TempProject):
    def _patched(self):
        return patched(ART_DIR=self.art,
                       ILLUST_DIR=self.art / "03_illust",
                       ILLUST_MANIFEST_FILE=self.art / "03_illust" / "manifest.json")

    def test_rejects_empty_payload(self):
        with self._patched():
            with self.assertRaises(ValueError):
                studio.archive_illust(b"", "a.png", {})

    def test_rejects_unsupported_suffix(self):
        with self._patched():
            with self.assertRaises(ValueError):
                studio.archive_illust(b"data", "a.gif", {})

    def test_writes_file_and_manifest_without_overwriting(self):
        with self._patched():
            first = studio.archive_illust(b"PNGDATA", "shot.png", {"seed": 7})
            second = studio.archive_illust(b"PNGDATA2", "shot.png", {})
            manifest = json.loads(
                (self.art / "03_illust" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first["file"], "03_illust/final/shot.png")
        self.assertEqual(second["file"], "03_illust/final/shot-2.png")
        self.assertEqual(len(manifest["entries"]), 2)
        self.assertEqual(manifest["entries"][0]["seed"], 7)
        self.assertEqual(manifest["entries"][1]["bytes"], len(b"PNGDATA2"))


# --------------------------------------------------------------------------- #
# bible
# --------------------------------------------------------------------------- #
class TestSaveBible(TempProject):
    def test_merges_and_preserves_unknown_keys(self):
        path = self.art / "00_bible" / "character.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "trigger": "old", "notes": "keep me", "palette": {"main": "#111111"},
        }), encoding="utf-8")

        with patched(BIBLE_DIR=path.parent, BIBLE_FILE=path):
            studio.save_bible({"trigger": "char_x", "hair": "long hair",
                               "palette": {"accent": "#ff0000"}})

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["trigger"], "char_x")
        self.assertEqual(data["hair"], "long hair")
        self.assertEqual(data["notes"], "keep me")
        self.assertEqual(data["palette"]["main"], "#111111")
        self.assertEqual(data["palette"]["accent"], "#ff0000")
        self.assertIn("updated_at", data)

    def test_editor_state_reports_missing_required_fields(self):
        with patched(BIBLE_DIR=self.art / "00_bible",
                     BIBLE_FILE=self.art / "00_bible" / "character.json"):
            state = studio.bible_editor_state()
        self.assertFalse(state["exists"])
        self.assertFalse(state["complete"])
        self.assertEqual(sorted(state["missing"]), sorted(studio.BIBLE_REQUIRED))


# --------------------------------------------------------------------------- #
# prompts only
# --------------------------------------------------------------------------- #
class TestRenderPrompt(unittest.TestCase):
    PROMPTS = {"templates": {"t": {
        "stage": 1, "title": "T", "placeholders": ["a"],
        "positive": "hello {{a}}", "negative": "",
    }}}

    def test_substitutes_values(self):
        result = studio.render_prompt("t", {"a": "world"}, self.PROMPTS)
        self.assertEqual(result["positive"], "hello world")
        self.assertEqual(result["missing"], [])

    def test_unknown_template_raises(self):
        with self.assertRaises(KeyError):
            studio.render_prompt("nope", {}, self.PROMPTS)

    def test_missing_placeholders_are_listed(self):
        self.assertEqual(studio.render_prompt("t", {}, self.PROMPTS)["missing"], ["a"])

    def test_placeholders_of_is_sorted_and_unique(self):
        self.assertEqual(studio.placeholders_of("{{b}} {{a}} {{b}}"), ["a", "b"])


# --------------------------------------------------------------------------- #
# env parsing
# --------------------------------------------------------------------------- #
class TestEnvHelpers(unittest.TestCase):
    def test_comfyui_url_adds_scheme(self):
        self.assertEqual(studio.comfyui_url({"STUDIO_COMFYUI_HOST": "127.0.0.1:8188"}),
                         "http://127.0.0.1:8188")

    def test_console_port_falls_back(self):
        self.assertEqual(studio.console_port({"STUDIO_CONSOLE_PORT": "9999"}), 9999)
        self.assertEqual(studio.console_port({"STUDIO_CONSOLE_PORT": "bad"}), 8766)
        self.assertEqual(studio.console_port({}), 8766)


# --------------------------------------------------------------------------- #
# comfy job status (history/queue stubbed, no network)
# --------------------------------------------------------------------------- #
class TestJobStatus(unittest.TestCase):
    def _stub(self, history=None, queue=None):
        self._orig = (comfy.history, comfy.queue)
        comfy.history = lambda prompt_id: (history or {}).get(prompt_id, {})
        comfy.queue = lambda: (queue or {"running": [], "pending": []})

    def tearDown(self):
        if hasattr(self, "_orig"):
            comfy.history, comfy.queue = self._orig

    def test_done_collects_outputs_and_elapsed(self):
        self._stub(history={"p1": {
            "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "s",
                                          "type": "output"}]}},
            "status": {"status_str": "success", "messages": [
                ["execution_start", {"timestamp": 1000}],
                ["execution_success", {"timestamp": 3500}]]},
        }})
        status = comfy.job_status("p1")
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["outputs"][0]["filename"], "a.png")
        self.assertEqual(status["elapsed_s"], 2.5)

    def test_error_reports_node_message(self):
        self._stub(history={"p2": {
            "outputs": {},
            "status": {"status_str": "error", "messages": [
                ["execution_error", {"node_type": "KSampler",
                                     "exception_message": "CUDA out of memory"}]]},
        }})
        status = comfy.job_status("p2")
        self.assertEqual(status["state"], "error")
        self.assertIn("CUDA out of memory", status["message"])

    def test_running_and_queued_positions(self):
        self._stub(history={}, queue={"running": [[1, "p3"]], "pending": [[2, "p4"]]})
        self.assertEqual(comfy.job_status("p3")["state"], "running")
        queued = comfy.job_status("p4")
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["position"], 1)

    def test_unknown_prompt(self):
        self._stub(history={}, queue={"running": [], "pending": []})
        self.assertEqual(comfy.job_status("nope")["state"], "unknown")


# --------------------------------------------------------------------------- #
# remediation + local models
# --------------------------------------------------------------------------- #
class TestRemediation(unittest.TestCase):
    def test_pass_findings_have_no_fix(self):
        self.assertIsNone(remediation.plan({"id": "NET-01", "status": "pass"}))

    def test_action_mapping(self):
        fix = remediation.plan({"id": "SVC-04", "status": "warn", "evidence": "x"})
        self.assertEqual(fix["kind"], "action")
        self.assertEqual(fix["action"], "dedupe_comfy")

    def test_copy_pulls_evidence(self):
        fix = remediation.plan({"id": "MDL-04", "status": "warn",
                                "evidence": "缺少 a, b"})
        self.assertEqual(fix["kind"], "copy")
        self.assertEqual(fix["text"], "缺少 a, b")

    def test_unknown_finding_has_no_fix(self):
        self.assertIsNone(remediation.plan({"id": "ZZZ-99", "status": "fail"}))

    def test_annotate_attaches_only_fixable(self):
        items = [{"id": "COD-01", "status": "fail", "evidence": "x"},
                 {"id": "ENV-01", "status": "pass", "evidence": "y"}]
        remediation.annotate(items)
        self.assertIn("fix", items[0])
        self.assertNotIn("fix", items[1])

    def test_every_fix_is_well_formed(self):
        for cid, fix in remediation.FIXES.items():
            self.assertIn(fix["kind"], ("action", "copy", "goto"), cid)
            self.assertTrue(fix.get("label"), cid)
            if fix["kind"] == "action":
                self.assertTrue(fix.get("action"), cid)
            if fix["kind"] == "goto":
                self.assertIn(fix.get("tab"), ("home", "studio", "gallery",
                                               "models", "health"), cid)


class TestOllamaClient(unittest.TestCase):
    def test_base_url_defaults_and_scheme(self):
        self.assertEqual(ollama.base_url({}), "http://127.0.0.1:11434")
        self.assertEqual(ollama.base_url({"STUDIO_OLLAMA_HOST": "127.0.0.1:1234"}),
                         "http://127.0.0.1:1234")
        self.assertEqual(ollama.base_url({"STUDIO_OLLAMA_HOST": "http://host:1"}),
                         "http://host:1")

    def test_snapshot_merges_loaded_models(self):
        def fake(path, payload=None, timeout=30):
            if path == "/api/tags":
                return {"models": [{"name": "a:latest", "size": 1000,
                                    "details": {"parameter_size": "9B",
                                                "quantization_level": "Q4_K_M"}}]}
            if path == "/api/ps":
                return {"models": [{"name": "a:latest", "size_vram": 500,
                                    "size": 1000, "expires_at": "2026-10-30T00:00:00Z"}]}
            raise AssertionError(path)

        original = ollama._request
        ollama._request = fake
        try:
            snap = ollama.snapshot()
        finally:
            ollama._request = original
        self.assertTrue(snap["online"])
        self.assertEqual(snap["vram_bytes"], 500)
        self.assertTrue(snap["models"][0]["loaded"])
        self.assertEqual(snap["models"][0]["params"], "9B")

    def test_snapshot_offline(self):
        def boom(*args, **kwargs):
            raise ollama.OllamaError("down")

        original = ollama._request
        ollama._request = boom
        try:
            snap = ollama.snapshot()
        finally:
            ollama._request = original
        self.assertFalse(snap["online"])
        self.assertEqual(snap["models"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
