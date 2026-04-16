"""测试 Agent 结构化输出模型。"""

from engine.agent_schema import MAX_CHOICE_CHARS, ChoicesOutput


def test_choices_output_trims_each_choice_to_50_chars():
    long_choice = "我" * (MAX_CHOICE_CHARS + 8)

    output = ChoicesOutput(choices=[f"  {long_choice}  ", "短选项"])

    assert output.choices[0] == "我" * MAX_CHOICE_CHARS
    assert len(output.choices[0]) == MAX_CHOICE_CHARS
    assert output.choices[1] == "短选项"
