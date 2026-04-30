"""测试长期记忆索引用文本的字段组合。"""

from memory.parser import EpisodeMemory
from storage.vector_store import VectorStore


def test_embed_text_includes_title_keywords_and_content():
    episode = EpisodeMemory(
        title="亲昵称呼约定",
        keywords=["小狗", "主人", "亲密称呼"],
        content="他叫我小狗，我叫他主人，这个称呼让互动变得更亲昵。",
        memory_owner="mitsuki",
        date="10月19日",
    )

    embed_text = VectorStore._embed_text(episode)

    assert "亲昵称呼约定" in embed_text
    assert "小狗、主人、亲密称呼" in embed_text
    assert "他叫我小狗" in embed_text
