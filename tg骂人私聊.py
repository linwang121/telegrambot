import time
import asyncio
from telethon import TelegramClient, errors

# ============================================================
# ★★★ 要发的消息，全部写在这里，一行一条 ★★★
# 加一行 = 多发一条，删一行 = 少发一条，顺序就是发送顺序
# ============================================================
MESSAGES = [
    "儿子为什么怕了 垃圾扣子",
    "你什么样的废物",
    "你就民主似的安静,",
    "垃圾随便接着叫",
    "你就这样可以滚了,",
    "你这样的废物,",
    "是不是沉默是金,,",
    "你告诉我为什么",
    "你为什么要操你妈",
    "你为什么...操你妈大傻逼",
    "二逼青年。",
    "我是你爷爷。",
    "和我桀骜呢。",
    "没头脑的傻。",
    "脑袋瓜子栓起来。",
    "我就捆起来。",
    "山旮旯出来的。",
    "是不是你呢。",
    "你和黑界出来的。",
    "这个傻似的。",
    "活生生黑界出来。",
    "傻滚刀肉似的。",
    "各式各样发挥。",
    "农村挑大梁的。",
    "双管齐下草你妈。",
    "直截了当过去。",
    "直接操死你妈。",
    "怎么着了呢。",
    "好象白日做梦。",
    "你的老妈子。",
    "这个输卵管。",
    "我可以拉出来。",
    "我左右开弓呢。",
    "迫不及待。",
    "好象没脾气。",
    "没有了脾气。"
]
# ============================================================


# ===== 固定配置（不要动）=====
API_ID = 20655624
API_HASH = "9950250e33de542aed737c114e97ac71"

PHONE_NUMBER = ""
TARGET_USERNAME = ""


async def send_private_messages(client, target_username, texts):
    if not texts:
        print("没有可发送的文本内容！")
        return

    try:
        target_user = await client.get_entity(target_username)
    except ValueError:
        print(f"错误：未找到用户 {target_username}，请确认用户名/ID正确")
        return
    except Exception as e:
        print(f"获取用户出错：{e}")
        return

    print(f"\n开始私信发送（共{len(texts)}条），目标用户：{target_username}\n")

    for idx, text in enumerate(texts, 1):
        try:
            await client.send_message(entity=target_user, message=text)
            print(f"第{idx}条发送成功：{text}")
            time.sleep(1)
        except errors.FloodWaitError as e:
            print(f"频率限制，需等待{e.seconds}秒后重试...")
            time.sleep(e.seconds)
            try:
                await client.send_message(entity=target_user, message=text)
                print(f"第{idx}条重试成功：{text}")
            except Exception as e2:
                print(f"第{idx}条重试失败：{e2}")
            time.sleep(1)
        except Exception as e:
            print(f"第{idx}条发送失败：{str(e)}")
            continue

    print("\n所有私信发送完成！")


def collect_inputs():
    global PHONE_NUMBER, TARGET_USERNAME

    print("=" * 40)
    print("       Telegram 私信群发工具")
    print("=" * 40)

    while True:
        phone = input("请输入你的 TG 手机号（带国家码，如 +8613800138000）：").strip()
        if phone:
            PHONE_NUMBER = phone
            break
        print("手机号不能为空，请重新输入。")

    while True:
        target = input("请输入目标用户（@用户名 或 用户ID）：").strip()
        if target:
            TARGET_USERNAME = target
            break
        print("目标用户不能为空，请重新输入。")

    print("\n----- 已确认信息 -----")
    print(f"手机号   : {PHONE_NUMBER}")
    print(f"目标用户 : {TARGET_USERNAME}")
    print(f"待发消息 : {len(MESSAGES)} 条（写在代码里）")
    print("----------------------\n")


async def main():
    collect_inputs()

    client = TelegramClient(PHONE_NUMBER, API_ID, API_HASH)
    try:
        await client.start(phone=PHONE_NUMBER)
        if await client.is_user_authorized():
            print(f"登录成功！当前账号：{PHONE_NUMBER}\n")
        else:
            print("登录失败，请检查验证码")
            return
    except Exception as e:
        print(f"登录异常：{str(e)}")
        return

    await send_private_messages(client, TARGET_USERNAME, MESSAGES)
    await client.disconnect()
    print("已断开连接，程序退出。")


if __name__ == "__main__":
    asyncio.run(main())