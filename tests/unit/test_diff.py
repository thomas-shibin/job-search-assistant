

def test_side_by_side_diff_marks_removed_words_before_and_added_words_after():
    from app.modules.resumes.diff import side_by_side_diff

    before, after = side_by_side_diff("Owned the observability stack, reducing MTTD", "Owned the observability stack for Kafka")
    assert before == "Owned the observability <del>stack, reducing MTTD</del>"
    assert after == "Owned the observability <ins>stack for Kafka</ins>"
    assert side_by_side_diff(None, "New <b>")[1] == "New &lt;b&gt;"  # escaped
