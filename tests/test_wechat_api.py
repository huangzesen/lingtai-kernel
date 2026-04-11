import json
import pytest
import httpx

from lingtai.addons.wechat import api
from lingtai.addons.wechat.types import WeixinMessage, MessageItem, TextItem


@pytest.mark.anyio
async def test_get_qrcode(httpx_mock):
    httpx_mock.add_response(
        url="https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3",
        json={"qrcode": "qr123", "qrcode_img_content": "data:image/png;base64,..."},
    )
    result = await api.get_qrcode()
    assert result["qrcode"] == "qr123"


@pytest.mark.anyio
async def test_poll_qr_status_confirmed(httpx_mock):
    httpx_mock.add_response(
        json={
            "status": "confirmed",
            "bot_token": "tok123",
            "ilink_bot_id": "bot@im.bot",
            "ilink_user_id": "wxid@im.wechat",
        },
    )
    result = await api.poll_qr_status("https://ilinkai.weixin.qq.com", "qr123")
    assert result["status"] == "confirmed"
    assert result["bot_token"] == "tok123"


@pytest.mark.anyio
async def test_get_updates_with_messages(httpx_mock):
    httpx_mock.add_response(
        json={
            "ret": 0,
            "msgs": [
                {
                    "from_user_id": "wxid@im.wechat",
                    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                }
            ],
            "get_updates_buf": "buf2",
        },
    )
    resp = await api.get_updates("https://ilinkai.weixin.qq.com", "tok123")
    assert len(resp.msgs) == 1
    assert resp.msgs[0].from_user_id == "wxid@im.wechat"
    assert resp.get_updates_buf == "buf2"


@pytest.mark.anyio
async def test_get_updates_timeout():
    """On timeout, returns empty response with same buf."""
    # Use a very short timeout against a non-routable address
    resp = await api.get_updates(
        "http://192.0.2.1",  # RFC 5737 TEST-NET
        "tok", get_updates_buf="old_buf", timeout=0.1,
    )
    assert resp.msgs == []
    assert resp.get_updates_buf == "old_buf"


@pytest.mark.anyio
async def test_send_message(httpx_mock):
    httpx_mock.add_response(json={})
    msg = WeixinMessage(
        to_user_id="wxid@im.wechat",
        item_list=[MessageItem(type=1, text_item=TextItem(text="hi"))],
    )
    await api.send_message("https://ilinkai.weixin.qq.com", "tok123", msg)
    req = httpx_mock.get_request()
    body = json.loads(req.content)
    assert body["msg"]["to_user_id"] == "wxid@im.wechat"


@pytest.mark.anyio
async def test_get_config(httpx_mock):
    httpx_mock.add_response(
        json={"ret": 0, "typing_ticket": "ticket123"},
    )
    resp = await api.get_config("https://ilinkai.weixin.qq.com", "tok123")
    assert resp.typing_ticket == "ticket123"
