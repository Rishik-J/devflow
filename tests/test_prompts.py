from devflow import prompts

ARGS = ({"goal": "g"}, {"tests": []}, "facts", "stat", "abc1234", "DIFFBODY")


def test_blast_radius_block_and_position():
    out = prompts.reviewer_task(*ARGS, blast_radius="- [ ] Contract `f()`")
    assert "<<<BLAST RADIUS\n- [ ] Contract `f()`\nBLAST RADIUS>>>" in out
    assert prompts.BLAST_RADIUS_GUIDANCE in out
    assert "graph-derived" in out and "Tick or reject" in out and "blocking finding" in out
    assert out.index("DIFF STAT>>>") < out.index("<<<BLAST RADIUS") < out.index("<<<DIFF\n")


def test_default_prompt_is_unchanged():
    base = prompts.reviewer_task(*ARGS)
    assert prompts.reviewer_task(*ARGS, blast_radius=None) == base
    assert prompts.reviewer_task(*ARGS, blast_radius="") == base
    assert "BLAST RADIUS" not in base
