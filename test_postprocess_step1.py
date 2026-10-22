import re
from collections import Counter

# Simulate the exact structure from the user's "before postprocessing" content
test_content = """Saskatchewan&apos;s per capita emissions of <trace>36.97</trace>
<trace.deleted>55.8<trace.deleted/> tonnes carbon dioxide equivalent are <trace>10.25</trace>
<trace.deleted>9.8<trace.deleted/>&percnt; of Canada&apos;s <trace.deleted>total<trace.deleted/>
<trace>total carbon dioxide</trace> emission<trace>s</trace> despite having only 3&percnt; of the country&apos;s population.<trace.deleted/> That amounts to <trace.deleted>66<trace.deleted/>
<trace>46</trace> million tonnes of carbon dioxide equivalent emissions each year.<trace.deleted/>
<footnote date.trace="1"><footnote.reference>1</footnote.reference></footnote>
<trace> However, between 2005 and 2024</trace>"""

print("=== BEFORE Step 1 ===")
print(test_content)
print()

def _has_balanced_tags(content):
    opens = re.findall(r'<([a-zA-Z][a-zA-Z0-9._-]*)\b[^>]*(?<!/)>', content)
    closes = re.findall(r'</([a-zA-Z][a-zA-Z0-9._-]*)\s*>', content)
    open_counts = Counter(opens)
    close_counts = Counter(closes)
    open_counts.pop('trace.deleted', None)
    close_counts.pop('trace.deleted', None)
    return open_counts == close_counts

def _safe_collapse(m):
    content = m.group(1)
    if '<trace>' in content or '<trace ' in content:
        print(f"  SKIP (has <trace> insertions): {repr(content[:60])}...")
        return m.group(0)
    print(f"  COLLAPSE: {repr(content[:60])}")
    if _has_balanced_tags(content):
        return '<trace.deleted/>'
    return m.group(0)

# Two-pass approach (matches postprocessing.py logic)
# Pass A: Non-greedy, blocks ALL <trace.deleted in content (matches NEAREST closer)
pattern_A = r'<trace\.deleted>((?:(?!<trace\.deleted)[\s\S])*?)<trace\.deleted\s*/>'
# Pass B: Greedy, only blocks <trace.deleted> openers (allows self-closing markers through)
pattern_B = r'<trace\.deleted>((?:(?!<trace\.deleted[\s>])[\s\S])*)<trace\.deleted\s*/>'

step1_iter = 0
while True:
    step1_iter += 1
    prev_content = test_content
    print(f"\n--- Step 1 Iteration {step1_iter} ---")
    print("  Pass A (non-greedy, nearest closer):")
    test_content = re.sub(pattern_A, _safe_collapse, test_content)
    print("  Pass B (greedy, nested deletions):")
    test_content = re.sub(pattern_B, _safe_collapse, test_content)
    if test_content == prev_content:
        print("  No more matches.")
        break
    if step1_iter > 5:
        break

print("\n=== AFTER Step 1 ===")
print(test_content)

# Verify expected behavior
print("\n=== VERIFICATION ===")
assert '<trace>total carbon dioxide</trace>' in test_content, "FAIL: insertion content was dropped!"
assert '<trace>s</trace>' in test_content, "FAIL: insertion 's' was dropped!"
assert '<trace>46</trace>' in test_content, "FAIL: insertion '46' was dropped!"
assert '<trace> However, between 2005 and 2024</trace>' in test_content, "FAIL: tail insertion was dropped!"
assert '<trace.deleted>55.8<trace.deleted/>' not in test_content, "FAIL: '55.8' should have been collapsed!"
assert '<trace.deleted>9.8<trace.deleted/>' not in test_content, "FAIL: '9.8' should have been collapsed!"
assert '<trace.deleted>total<trace.deleted/>' not in test_content, "FAIL: 'total' should have been collapsed!"
assert '<trace.deleted>66<trace.deleted/>' not in test_content, "FAIL: '66' should have been collapsed!"
print("ALL ASSERTIONS PASSED ✓")
