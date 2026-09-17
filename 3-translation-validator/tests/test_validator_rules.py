#!/usr/bin/env python3
"""
Unit tests for 3-tier Practical Validator rules in grok_audit_proxy.py.
Validates:
  1. Normal translation PASS
  2. Exact repetition HARD FAIL
  3. Minor Hangul residual WARN (PASS)
  4. Minor English contamination WARN (PASS)
  5. Source Jamo preserved PASS
  6. Missing empty key auto-repair PASS
  7. Missing non-empty key HARD FAIL
  8. JSON syntax error HARD FAIL
  9. OPAQUE_LITERAL sound effect / glitch text PASS
"""

import sys
import os
import json

# Add parent directory to path so grok_audit_proxy can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from grok_audit_proxy import validate_and_repair_body


def test_normal_translation():
    source = {"0": "그는 조용히 문을 열고 안으로 들어갔다."}
    target = {"0": "他轻轻推开门，走了进去。"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected PASS but got {reason}"
    assert reason is None
    print("[PASS] test_normal_translation")


def test_exact_repetition_hard_fail():
    # Natural Korean sentences repeated verbatim must HARD FAIL
    source = {"0": "그는 조용히 문을 열고 안으로 들어갔다."}
    target = {"0": "그는 조용히 문을 열고 안으로 들어갔다."}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is False, "Expected HARD FAIL for exact Korean repetition"
    assert "raw_repetition_exact" in reason, f"Unexpected reason: {reason}"
    print("[PASS] test_exact_repetition_hard_fail")


def test_hangul_residual_warning():
    # Residual single Korean word in Chinese translation should WARN but PASS
    source = {"0": "그녀는 오빠에게 미소를 지었다."}
    target = {"0": "她对着오빠微微一笑。"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected PASS with WARN but got failure: {reason}"
    assert any("hangul_residual" in w for w in warnings), f"Expected hangul residual warning, got: {warnings}"
    print("[PASS] test_hangul_residual_warning")


def test_english_contamination_warning():
    # Residual English word (not in game terms/source) should WARN but PASS
    source = {"0": "그는 조용히 문을 열었다."}
    target = {"0": "他轻轻推开了 strawberry 门。"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected PASS with WARN but got failure: {reason}"
    assert any("english" in w for w in warnings), f"Expected english warning, got: {warnings}"
    print("[PASS] test_english_contamination_warning")


def test_jamo_preservation():
    # Source containing standalone Jamo (e.g. ㅋㅋㅋ, ㅎㅎㅎ) preserved or kept
    source = {"0": "진짜 웃기네 ㅋㅋㅋ"}
    target = {"0": "真搞笑 哈哈哈"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected PASS but got {reason}"
    print("[PASS] test_jamo_preservation")


def test_empty_key_auto_repair():
    # Missing empty line/key in target should be auto-repaired
    source = {"0": "첫 번째 줄", "1": "", "2": "세 번째 줄"}
    target = {"0": "第一行", "2": "第三行"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected auto-repair PASS but got: {reason}"
    repaired_obj = json.loads(repaired)
    assert repaired_obj.get("1") == "", "Key 1 was not restored as empty string"
    assert any("restored_empty_key_1" in r for r in repairs)
    print("[PASS] test_empty_key_auto_repair")


def test_missing_nonempty_key_hard_fail():
    # Missing meaningful key must HARD FAIL
    source = {"0": "첫 번째 줄", "1": "중요한 내용", "2": "세 번째 줄"}
    target = {"0": "第一行", "2": "第三行"}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is False, "Expected HARD FAIL for missing non-empty key"
    assert "missing_nonempty_key_1" in reason, f"Unexpected reason: {reason}"
    print("[PASS] test_missing_nonempty_key_hard_fail")


def test_invalid_json_hard_fail():
    source = {"0": "첫 번째 줄"}
    target_raw = "This is not valid json at all {"
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, target_raw)
    assert passed is False, "Expected HARD FAIL for invalid JSON"
    assert "json_parse_error" in reason
    print("[PASS] test_invalid_json_hard_fail")


def test_opaque_literal_pass():
    # Corrupted / glitch dialogue text in brackets should be rescued as OPAQUE_LITERAL
    sample = "(벨겟굶눋뜩뢰겆뀐걸흐흐흐)"
    source = {"0": sample}
    target = {"0": sample}
    passed, repaired, reason, repairs, warnings = validate_and_repair_body(source, json.dumps(target))
    assert passed is True, f"Expected OPAQUE_LITERAL PASS, got: {reason}"
    print("[PASS] test_opaque_literal_pass")


if __name__ == "__main__":
    test_normal_translation()
    test_exact_repetition_hard_fail()
    test_hangul_residual_warning()
    test_english_contamination_warning()
    test_jamo_preservation()
    test_empty_key_auto_repair()
    test_missing_nonempty_key_hard_fail()
    test_invalid_json_hard_fail()
    test_opaque_literal_pass()
    print("\nAll Practical Validator rule tests passed successfully!")
