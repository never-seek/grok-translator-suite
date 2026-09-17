import sys
import os

# Add parent directory to path to import grok_audit_proxy
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from grok_audit_proxy import is_opaque_literal

def test_natural_hangul_is_never_opaque():
    """Verify that ordinary natural Korean sentences NEVER pass as opaque literals (False Positive = 0)."""
    natural_samples = [
        "그는 문을 열었다.",
        "도망쳐! 어서!",
        "살려줘! 제발 부탁이야.",
        "아카데미의 훈련생들은 조용히 걸어갔다.",
        "식사를 마치고 숙소로 돌아왔다.",
        "“일주일 안으로 카야와 상담하는 날이 있을 거다.”",
        "“이게 무슨 말이야?”",
        "갑자기 눈앞이 캄캄해졌다."
    ]
    for sample in natural_samples:
        is_op, reason = is_opaque_literal(sample, sample)
        assert not is_op, f"False Positive detected! '{sample}' was misclassified as opaque: {reason}"

def test_html_interleaved_scripts_is_opaque():
    """Verify that broken speech with mixed HTML tags and jamo/latin is identified as opaque."""
    sample1 = "“이.`0ㅇ거 3$<i>ㅂ</i>ㄱP<i>사sg</i>K<u>ㄹ게</u>ㅔ<b><i>ㅛ</i></b>.”"
    sample2 = "“<b>O</b>I<i>ㄱu</i>R5도<b><u>lㄴ</u></b>ao?”"
    
    is_op1, reason1 = is_opaque_literal(sample1, sample1)
    is_op2, reason2 = is_opaque_literal(sample2, sample2)
    
    assert is_op1, f"Failed to identify opaque sample 1: {sample1}"
    assert is_op2, f"Failed to identify opaque sample 2: {sample2}"

def test_bracketed_unspaced_hangul_is_opaque():
    """Verify that system glitch or madness speech in brackets is identified as opaque."""
    sample = "(벨겟굶눋뜩뢰겆뀐걸흐흐흐)"
    is_op, reason = is_opaque_literal(sample, sample)
    assert is_op, f"Failed to identify bracketed unspaced hangul: {sample}"

def test_corrupted_speaker_dialogue_is_opaque():
    """Verify corrupted glitch speaker format is identified as opaque."""
    sample = "벨덱냇벋뒨됩흐흐 : 벨뜹꿨흐"
    is_op, reason = is_opaque_literal(sample, sample)
    assert is_op, f"Failed to identify corrupted dialogue speaker: {sample}"

def test_long_unspaced_hangul_is_opaque():
    """Verify abnormal non-natural long continuous hangul glitch is identified as opaque."""
    sample = "벨둡냈깖러뒝렌궁베똑냇렷록긱띰빕 베맏냈누룐뢰뚠꽤뤠릴띰빕"
    is_op, reason = is_opaque_literal(sample, sample)
    assert is_op, f"Failed to identify long unspaced token: {sample}"

if __name__ == '__main__':
    test_natural_hangul_is_never_opaque()
    test_html_interleaved_scripts_is_opaque()
    test_bracketed_unspaced_hangul_is_opaque()
    test_corrupted_speaker_dialogue_is_opaque()
    test_long_unspaced_hangul_is_opaque()
    print("All OPAQUE_LITERAL classifier tests passed successfully!")
