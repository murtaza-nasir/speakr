"""Markdown conversion from several request threads at once (#442).

One Markdown instance was shared by all threads; its state (footnotes,
abbreviations, the output tree) is reset and filled during a conversion, so
concurrent conversions returned each other's HTML.
"""
import threading

from src.utils.markdown import md_to_html


def _doc(n):
    return (f"# Summary {n}\n\nPoint {n} of the meeting.[^{n}]\n\n"
            f"*[ACME{n}]: Company {n}\n\nACME{n} agreed.\n\n[^{n}]: Footnote {n}.\n")


def test_concurrent_conversions_match_single_thread():
    expected = {n: md_to_html(_doc(n)) for n in range(8)}
    mismatches, errors = [], []

    def work(n):
        for _ in range(60):
            try:
                if md_to_html(_doc(n)) != expected[n]:
                    mismatches.append(n)
            except Exception as e:  # noqa: BLE001 - any exception is a failure here
                errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert not mismatches


def test_footnotes_do_not_carry_over():
    first = md_to_html("A[^1]\n\n[^1]: one")
    second = md_to_html("B without notes")
    assert "one" in first
    assert "footnote" not in second
