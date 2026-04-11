from lingtai.addons.wechat.types import (
    MessageItemType, WeixinMessage, MessageItem, TextItem,
    ImageItem, CDNMedia, VoiceItem, msg_from_dict, msg_to_dict,
)


def test_message_item_type_values():
    assert MessageItemType.TEXT == 1
    assert MessageItemType.IMAGE == 2
    assert MessageItemType.VOICE == 3
    assert MessageItemType.FILE == 4
    assert MessageItemType.VIDEO == 5


def test_msg_from_dict_text():
    raw = {
        "from_user_id": "wxid_abc@im.wechat",
        "to_user_id": "bot123@im.bot",
        "create_time_ms": 1700000000000,
        "context_token": "tok123",
        "item_list": [
            {"type": 1, "text_item": {"text": "hello"}}
        ],
    }
    msg = msg_from_dict(raw)
    assert msg.from_user_id == "wxid_abc@im.wechat"
    assert msg.context_token == "tok123"
    assert len(msg.item_list) == 1
    assert msg.item_list[0].type == MessageItemType.TEXT
    assert msg.item_list[0].text_item.text == "hello"


def test_msg_from_dict_image():
    raw = {
        "from_user_id": "wxid_abc@im.wechat",
        "item_list": [
            {
                "type": 2,
                "image_item": {
                    "media": {"full_url": "https://cdn.example.com/img.jpg"},
                    "aeskey": "abc123",
                },
            }
        ],
    }
    msg = msg_from_dict(raw)
    assert msg.item_list[0].type == MessageItemType.IMAGE
    assert msg.item_list[0].image_item.media.full_url == "https://cdn.example.com/img.jpg"
    assert msg.item_list[0].image_item.aeskey == "abc123"


def test_msg_from_dict_voice_with_transcription():
    raw = {
        "from_user_id": "wxid_abc@im.wechat",
        "item_list": [
            {
                "type": 3,
                "voice_item": {
                    "media": {"full_url": "https://cdn.example.com/voice.silk"},
                    "text": "transcribed text",
                    "playtime": 5000,
                },
            }
        ],
    }
    msg = msg_from_dict(raw)
    assert msg.item_list[0].voice_item.text == "transcribed text"
    assert msg.item_list[0].voice_item.playtime == 5000


def test_msg_to_dict_text():
    msg = WeixinMessage(
        from_user_id="bot@im.bot",
        to_user_id="wxid_abc@im.wechat",
        context_token="tok",
        item_list=[
            MessageItem(type=1, text_item=TextItem(text="hi")),
        ],
    )
    d = msg_to_dict(msg)
    assert d["from_user_id"] == "bot@im.bot"
    assert d["context_token"] == "tok"
    assert d["item_list"][0]["text_item"]["text"] == "hi"


def test_msg_from_dict_empty():
    msg = msg_from_dict({})
    assert msg.from_user_id is None
    assert msg.item_list == []


def test_roundtrip_text():
    raw = {
        "from_user_id": "wxid@im.wechat",
        "to_user_id": "bot@im.bot",
        "context_token": "ctx",
        "item_list": [{"type": 1, "text_item": {"text": "roundtrip"}}],
    }
    msg = msg_from_dict(raw)
    d = msg_to_dict(msg)
    assert d["from_user_id"] == "wxid@im.wechat"
    assert d["item_list"][0]["text_item"]["text"] == "roundtrip"
