from conftest import EXPECTED_PATCH, FIXTURE
from repodoctor.agent import FakeModel
from repodoctor.cli import main


def run(sandbox_factory, answers, tmp_path, **kwargs):
    lines, answers = [], iter(answers)
    code = main(
        [str(FIXTURE), "--engine", "fake"],
        sandbox_factory=sandbox_factory,
        engine=FakeModel([EXPECTED_PATCH.read_text(encoding="utf-8")]),
        input_fn=lambda prompt: next(answers),
        out=lines.append,
        **kwargs,
    )
    return code, "\n".join(lines)


def test_approving_prints_a_verified_report(sandbox_factory, tmp_path):
    code, text = run(sandbox_factory, ["y"], tmp_path)
    assert code == 0
    assert "PROPOSED PATCH" in text and "DEBUGGING COMPLETE" in text
    assert (
        "Before     : 1 failed / 31 passed" in text
        and "After      : 0 failed / 32 passed" in text
    )
    assert "Verified   : YES" in text and "app/payment.py" in text


def test_anything_but_yes_rejects_and_applies_nothing(sandbox_factory, tmp_path):
    for answer in ("n", "", "maybe"):
        code, text = run(sandbox_factory, [answer], tmp_path)
        assert code == 3 and "DEBUGGING COMPLETE" not in text and "Verified" not in text
    for sandbox in sandbox_factory.created:
        assert (
            "user_id"
            in (
                __import__("pathlib").Path(sandbox.workspace) / "app" / "payment.py"
            ).read_text()
        )


def test_fake_engine_needs_a_patch_file(capsys):
    assert main([str(FIXTURE), "--engine", "fake"]) == 2
    assert "--fake-patch" in capsys.readouterr().out


def test_bedrock_engine_without_a_model_id_explains_how_to_fix_it(monkeypatch, capsys):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    assert main([str(FIXTURE)]) == 2
    assert "BEDROCK_MODEL_ID is not set" in capsys.readouterr().out
