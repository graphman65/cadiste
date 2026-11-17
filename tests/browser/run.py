"""Real Chromium/served-UI regression runner. Uses the optional Python screenshots extra.

Run after building the frontend: npm --prefix web run test:browser.
--url may point at a server already serving tests/browser/cadiste.json.
All mutations use ordinary UI controls; inspection and screenshots use the read-only capture protocol.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent
INSPECT = "() => window.cadisteCapture.inspect()"


@contextmanager
def fixture_server(url):
    if url:
        yield url.rstrip("/")
        return
    with tempfile.TemporaryDirectory(prefix="cadiste-browser-") as directory:
        root = Path(directory)
        for name in ("model.py", "cadiste.json"):
            shutil.copy2(FIXTURE / name, root / name)
        ready = root / "ready.json"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), environment.get("PYTHONPATH")]))
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cadiste",
                    "serve",
                    "--profile",
                    str(root / "cadiste.json"),
                    "--port",
                    "0",
                    "--cache-dir",
                    str(root / "cache"),
                    "--ready-file",
                    str(ready),
                ],
                cwd=root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 60
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError(f"Fixture server did not become ready:\n{log.read()}")
                    time.sleep(0.05)
                yield json.loads(ready.read_text())["url"]
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def ready(page, kind, name):
    page.wait_for_function(
        """([kind, name]) => {
      const s = window.cadisteCapture?.inspect();
      return s?.ready && s.status === 'ok' && s.target?.kind === kind && s.target?.name === name &&
        s.viewport?.width > 0 && s.viewport?.canvas_width > 0;
    }""",
        arg=[kind, name],
        timeout=240000,
    )
    return page.evaluate(INSPECT)


def wait_inspection(page, expression):
    page.wait_for_function(f"() => {{ const s = window.cadisteCapture.inspect(); return {expression}; }}")
    return page.evaluate(INSPECT)


def assert_same_camera(before, after):
    """Compare pose/zoom, not incidental quaternion sign or floating-point serialization."""
    for field in ("position", "target"):
        assert all(
            math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
            for a, b in zip(before[field], after[field], strict=True)
        ), {"field": field, "before": before, "after": after}
    first, second = before["quaternion"], after["quaternion"]
    scale = math.sqrt(sum(value * value for value in first) * sum(value * value for value in second))
    assert scale > 0
    cosine = abs(sum(a * b for a, b in zip(first, second, strict=True))) / scale
    assert abs(1 - cosine) <= 1e-12, {"before": before, "after": after}
    assert math.isclose(before["zoom"], after["zoom"], rel_tol=1e-10, abs_tol=1e-10)


def capture(page):
    state = page.evaluate(INSPECT)
    return page.evaluate(
        """request => window.cadisteCapture.call(request)""",
        {
            "id": uuid.uuid4().hex,
            "method": "screenshot",
            "params": {"views": ["current"], "expected": {"target": state["target"], "build_id": state["build_id"]}},
        },
    )


def assert_text_selection(page, locator):
    """Drag-select rendered app text, copy natively, and keep plain C out of snapshot capture."""
    locator.scroll_into_view_if_needed()
    points = locator.evaluate(
        """element => {
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode()) && !node.textContent.trim()) {}
            if (!node) throw new Error("No consumer text to select");
            const start = node.textContent.search(/\\S/);
            const end = Math.min(node.textContent.length, start + 24);
            const range = document.createRange();
            range.setStart(node, start);
            range.setEnd(node, start + 1);
            const first = range.getBoundingClientRect();
            range.setStart(node, end - 1);
            range.setEnd(node, end);
            const last = range.getBoundingClientRect();
            return {
                start: {x: first.left + 0.2, y: first.top + first.height / 2},
                end: {x: last.right - 0.2, y: last.top + last.height / 2},
            };
        }"""
    )
    page.mouse.move(points["start"]["x"], points["start"]["y"])
    page.mouse.down()
    page.mouse.move(points["end"]["x"], points["end"]["y"], steps=12)
    page.mouse.up()
    selected = page.evaluate("window.getSelection().toString()")
    assert selected.strip() and selected in locator.inner_text(), "Dragging did not select the displayed app text"
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=page.evaluate("location.origin"))
    page.evaluate("navigator.clipboard.writeText('cadiste native copy sentinel')")
    page.keyboard.press("Meta+c" if sys.platform == "darwin" else "Control+c")
    assert page.evaluate("navigator.clipboard.readText()") == selected, "Native copy did not preserve selected text"
    locator.evaluate(
        """element => document.addEventListener("keydown", event => {
            element.dataset.selectionShortcutPrevented = String(event.defaultPrevented);
        }, {once: true})"""
    )
    page.keyboard.press("c")
    assert locator.get_attribute("data-selection-shortcut-prevented") == "false", "Plain C hijacked selected text"
    assert page.evaluate("window.getSelection().toString()") == selected
    locator.evaluate("element => delete element.dataset.selectionShortcutPrevented")
    page.evaluate("window.getSelection().removeAllRanges()")
    return selected


def run(page, url, output):
    scenarios = []
    pending = []
    # Hold the actual first trace response, not fake geometry, to make initial-loading assertions deterministic.
    page.route("**/api/trace?*", lambda route: pending.append(route))
    page.goto(url + "/?harness=1#part=Block", wait_until="domcontentloaded")
    page.wait_for_function("window.cadisteCapture?.inspect !== undefined")
    loading = wait_inspection(page, "s.status === 'loading' && !s.ready")
    assert loading["camera"] is None
    assert page.locator(".topbar").is_visible()
    for route in pending:
        route.continue_()
    page.unroute("**/api/trace?*")
    initial = ready(page, "part", "Block")
    assert initial["timings"]["render_ms"] is not None
    assert not initial["errors"], initial["errors"]
    scenarios.append("initial loading and deterministic renderer readiness")

    topbar_height = page.locator(".topbar").bounding_box()["height"]
    tree = page.get_by_role("tree", name="Block features")
    tree.get_by_role("treeitem").first.click()
    wait_inspection(page, "s.selection.length === 1 && s.selection[0].kind === 'feature'")
    page.get_by_role("button", name="Clear selection", exact=True).click()
    canvas = page.locator(".cad-view canvas:visible").first
    box = canvas.bounding_box()
    picked = False
    for x, y in [(0.4, 0.5), (0.6, 0.5), (0.5, 0.4), (0.5, 0.6), (0.35, 0.4), (0.65, 0.6)]:
        canvas.click(position={"x": box["width"] * x, "y": box["height"] * y})
        if any(item["kind"] == "entity" for item in page.evaluate(INSPECT)["selection"]):
            picked = True
            break
    assert picked, "Ordinary canvas picks never produced an exact entity selection"
    assert page.locator(".topbar").bounding_box()["height"] == topbar_height
    scenarios.append("entity picks remain distinct from feature selection without layout shift")

    page.get_by_role("button", name="Fixture", exact=True).click()
    ready(page, "assembly", "Fixture")
    page.get_by_role("tab", name="Instances", exact=True).click()
    rows = page.get_by_role("tree", name="Assembly instances")
    block_b = rows.locator('[data-id$="/block-b"]')
    block_a = rows.locator('[data-id$="/block-a"]')
    peg = rows.locator('[data-id$="/peg"]')
    path_b = block_b.get_attribute("data-id")
    path_a = block_a.get_attribute("data-id")
    path_peg = peg.get_attribute("data-id")
    assert path_a and path_b and path_peg
    block_b.locator("button.eye:not(.solo)").click()
    wait_inspection(page, f"s.visibility_layers.hidden_instances.includes({json.dumps(path_b)})")
    page.get_by_role("tab", name="Parts", exact=True).click()
    parts = page.get_by_role("listbox", name="Assembly parts")
    parts.locator('[data-part="Peg"] button.eye:not(.solo)').click()
    parts.get_by_role("button", name="Solo Block", exact=True).click()
    wait_inspection(page, "s.visibility_layers.solo_part === 'Block'")
    page.get_by_role("tab", name="Instances", exact=True).click()
    rows.get_by_role("button", name="Solo block-b", exact=True).click()
    both = wait_inspection(page, f"s.visibility_layers.solo_instance === {json.dumps(path_b)}")
    assert both["visibility_layers"]["solo_part"] == "Block"
    assert both["visibility"][path_a] == [0, 0]
    assert both["visibility"][path_b][0] != 0
    rows.get_by_role("button", name="Solo block-b", exact=True).click()
    page.get_by_role("tab", name="Parts", exact=True).click()
    parts.get_by_role("button", name="Solo Block", exact=True).click()
    restored = wait_inspection(
        page, "s.visibility_layers.solo_part === null && s.visibility_layers.solo_instance === null"
    )
    assert path_b in restored["visibility_layers"]["hidden_instances"]
    assert path_peg in restored["visibility_layers"]["hidden_parts"]
    assert restored["visibility"][path_a][0] != 0
    scenarios.append("part and instance solo layers independently override and restore their own eyes")

    page.get_by_title("Section: cut the scene", exact=False).click()
    page.get_by_role("button", name="Viewport", exact=True).click()
    sectioned = wait_inspection(page, "s.section.active && s.section.section?.plane.label === 'Viewport plane'")
    plane = sectioned["section"]["section"]["plane"]
    assert math.isclose(sum(value * value for value in plane["normal"]), 1, abs_tol=1e-8)
    assert all(
        math.isclose(a, b, abs_tol=1e-8) for a, b in zip(plane["point"], sectioned["camera"]["target"], strict=True)
    )
    page.get_by_role("spinbutton", name="Offset along the normal (mm)").fill("0.5")
    wait_inspection(page, "s.section.section.offset === 0.5")
    viewport_before = page.evaluate(INSPECT)["viewport"]["width"]
    separator = page.locator(".sidebar > .splitter-x")
    handle = separator.bounding_box()
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] + 80, handle["y"] + handle["height"] / 2, steps=8)
    page.mouse.up()
    resized = wait_inspection(page, f"s.viewport.width < {viewport_before - 40}")
    assert resized["timings"]["resize_at"] is not None
    scenarios.append("viewport-aligned section and sidebar ResizeObserver resizing")

    page.get_by_role("tab", name="Instances", exact=True).click()
    block_a.locator(".title").click()
    before_return = page.evaluate(INSPECT)
    block_a.get_by_role("button", name="Open", exact=True).click()
    ready(page, "part", "Block")
    page.get_by_role("button", name="Back to Fixture", exact=True).click()
    returned = ready(page, "assembly", "Fixture")
    assert_same_camera(before_return["camera"], returned["camera"])
    assert returned["section"] == before_return["section"]
    assert returned["selection"] == before_return["selection"]
    assert sorted(returned["hidden"]) == sorted(before_return["hidden"])
    page.get_by_title("Close the section", exact=True).click()
    scenarios.append("assembly return restores camera, section, canonical selection and effective visibility")

    # Hide the only shown instance through a real eye; retain one image with explicit suspicious-empty evidence.
    block_a.locator("button.eye:not(.solo)").click()
    wait_inspection(page, "Object.values(s.visibility).every(v => v[0] === 0 && v[1] === 0)")
    empty_before = page.evaluate(INSPECT)
    empty = capture(page)
    assert empty["images"][0]["diagnostics"]["suspicious_empty"], empty["images"][0]["diagnostics"]
    assert empty["images"][0]["diagnostics"]["warnings"]
    empty_after = page.evaluate(INSPECT)
    assert len(empty["images"]) == 1
    assert_same_camera(empty_before["camera"], empty_after["camera"])
    assert empty_before["timings"]["render_started_at"] == empty_after["timings"]["render_started_at"]
    block_a.locator("button.eye:not(.solo)").click()
    wait_inspection(page, f"s.visibility[{json.dumps(path_a)}][0] !== 0")
    scenarios.append("suspicious-empty screenshot is diagnosed and retained without a retry")

    page.get_by_role("button", name="Export", exact=True).click()
    page.get_by_role("checkbox", name="Verify native Bambu import/save in isolated settings").check()
    with page.expect_response("**/api/manufacturing/preflight") as response:
        page.get_by_role("menuitem", name="Manufacturing preflight", exact=False).click()
    report = response.value.json()
    assert response.value.ok, report
    assert report["object_count"] == 1, report
    page.get_by_text("Manufacturing preflight complete.", exact=True).wait_for()
    assert_text_selection(page, page.get_by_text("Manufacturing preflight complete.", exact=True))
    scenarios.append("rendered status drag-selection, native clipboard copy and unclaimed plain C")
    with (
        page.expect_download() as download,
        page.expect_response(
            lambda response: response.request.method == "POST" and urlparse(response.url).path == "/api/export"
        ) as response,
    ):
        page.get_by_role("menuitem", name="3MF", exact=False).click()
    exported = response.value
    assert exported.ok
    assert set(exported.request.post_data_json.get("hidden", [])) == {path_b, path_peg}
    destination = output / "filtered.3mf"
    download.value.save_as(destination)
    with zipfile.ZipFile(destination) as archive:
        model = ElementTree.fromstring(archive.read("3D/3dmodel.model"))
    namespace = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    meshes = [obj for obj in model.findall(".//m:object", namespace) if obj.find("m:mesh", namespace) is not None]
    assert len(meshes) == 1, "Export included hidden instances"
    page.get_by_role("button", name="Export", exact=True).click()
    scenarios.append("manufacturing preflight and real 3MF download apply effective visibility filtering")

    page.locator(".workflow-tools > summary").click()
    page.get_by_role("button", name="Pin baseline", exact=True).click()
    page.get_by_role("button", name="Compare baseline", exact=True).wait_for(state="visible")
    page.get_by_role("button", name="Compare baseline", exact=True).click()
    comparison = page.get_by_role("dialog", name="Baseline side-by-side review")
    comparison.wait_for()
    assert comparison.get_by_role("img").count() == 2
    page.wait_for_function(
        "Array.from(document.querySelectorAll('.baseline-review img')).every(i => i.complete && i.naturalWidth > 0)"
    )
    comparison.get_by_role("button", name="Close baseline review").click()
    ready(page, "assembly", "Fixture")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/api/baselines")
    ) as released:
        page.get_by_role("button", name="Release baseline", exact=True).click()
    assert released.value.ok, released.value.text()
    scenarios.append("pin, same-camera side-by-side baseline review and release through ordinary controls")

    page.get_by_role("combobox", name="Mechanical check set", exact=True).select_option("witness-review")
    with page.expect_response("**/api/verify") as checked:
        page.get_by_role("button", name="Run checks", exact=True).click()
    assert checked.value.ok, checked.value.text()
    evidence = checked.value.json()["results"][0]["checks"][0]["result"]
    assert not evidence["ok"] and len(evidence["failing_poses"]) == 2
    review = page.locator(".check-review")
    recorded_camera = page.evaluate(INSPECT)["camera"]
    review.get_by_role("button").filter(has_text="Fail · shift-two").click()
    first = wait_inspection(page, "s.check_review?.label === 'shift-two'")
    transform = first["check_review"]["transforms"][0]
    original_matrix = transform["recorded_matrix_world"]
    expected = list(original_matrix)
    expected[12] += 2
    assert all(math.isclose(a, b, abs_tol=1e-7) for a, b in zip(transform["matrix_world"], expected, strict=True))
    assert first["visibility"][path_b][0] != 0, "Review must show the checked pair without rewriting its eyes"
    assert path_b in first["hidden"]
    page.wait_for_function("document.querySelectorAll('.measure-marks circle').length >= 2")
    review.get_by_role("button").filter(has_text="Fail · turn-and-shift-five").click()
    second = wait_inspection(page, "s.check_review?.label === 'turn-and-shift-five'")
    transformed = second["check_review"]["transforms"][0]
    assert transformed["recorded_matrix_world"] == original_matrix, (
        "Samples accumulated instead of starting at the recorded pose"
    )
    expected = list(original_matrix)
    for column in (0, 4, 8):
        expected[column] = -original_matrix[column + 1]
        expected[column + 1] = original_matrix[column]
    origin = second["check_review"]["poses"][0]["origin"]
    expected[12] = origin[0] - (original_matrix[13] - origin[1]) + 5
    expected[13] = origin[1] + original_matrix[12] - origin[0]
    assert all(math.isclose(a, b, abs_tol=1e-7) for a, b in zip(transformed["matrix_world"], expected, strict=True))
    assert_same_camera(recorded_camera, second["camera"])
    preview_capture = capture(page)
    assert any("pose preview" in warning for warning in preview_capture["images"][0]["diagnostics"]["warnings"])
    review.get_by_role("button", name="Return to recorded pose", exact=True).click()
    cleared = wait_inspection(page, "s.check_review === null")
    assert cleared["visibility"][path_b] == [0, 0]
    assert_same_camera(recorded_camera, cleared["camera"])
    scenarios.append(
        "exact-check witnesses, nonaccumulating failing rigid poses, world pivot and visibility restoration"
    )
    final = page.evaluate(INSPECT)
    assert not final["errors"], final["errors"]
    return {"scenarios": scenarios, "renderer": final}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="existing server serving the included fixture profile")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".cache" / "browser-regression")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise SystemExit("Install cadiste[screenshots], then python -m playwright install chromium") from error
    args.output.mkdir(parents=True, exist_ok=True)
    with fixture_server(args.url) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not args.headed,
            args=[
                "--enable-webgl",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
            ],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page = context.new_page()
        page.set_default_timeout(30000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            report = run(page, url, args.output)
            assert not errors, errors
            (args.output / "report.json").write_text(json.dumps(report, indent=2))
            print(f"PASS: {len(report['scenarios'])} real-browser scenarios; evidence: {args.output}")
        except Exception:
            page.screenshot(path=str(args.output / "failure.png"))
            (args.output / "failure.json").write_text(
                json.dumps(
                    {
                        "browser_errors": errors,
                        "renderer": page.evaluate("() => window.cadisteCapture?.inspect?.() ?? null"),
                    },
                    indent=2,
                )
            )
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
