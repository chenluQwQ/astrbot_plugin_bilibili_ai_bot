"""原 ``BilibiliAPIMixin`` 的样子：测试里的假插件继承 ``BiliParts``，就挂上 B站的几组接口。

真插件在 ``core/assembly.py`` 里建好 ``bili_auth``、``bili_wbi``、``bili_comments``、``bili_messages``、
``bili_users``、``bili_videos``、``bili_dynamics``；假插件没走装配，第一次用到时现建。假插件类上写了同名方法的
（``_get_video_info`` 这类），建对象时挂到对象上（见 ``parts.preferring``）。
"""
from importlib import import_module

from base_parts import attach_base, base_parts
from parts import preferring

# 原来 mixin 上转发的名字 → 现在在哪组接口上
OWNERS = {
    "_ensure_buvid": "bili_auth",
    "check_cookie": "bili_auth",
    "check_need_refresh": "bili_auth",
    "_generate_correspond_path": "bili_auth",
    "generate_qr": "bili_auth",
    "poll_qr_status": "bili_auth",
    "refresh_cookie": "bili_auth",
    "_qr_login_generate": "bili_auth",
    "_qr_login_poll": "bili_auth",
    "_get_wbi_keys": "bili_wbi",
    "_get_mixin_key": "bili_wbi",
    "sign_wbi_params": "bili_wbi",
    "_wbi_get": "bili_wbi",
    "_send_reply": "bili_comments",
    "_strip_at_prefix": "bili_comments",
    "_send_comment": "bili_comments",
    "_get_hot_comments": "bili_comments",
    "_get_comment_images": "bili_comments",
    "_send_bili_private_payload": "bili_messages",
    "_send_bili_private_message": "bili_messages",
    "_send_bili_private_video_share": "bili_messages",
    "get_followings": "bili_users",
    "_follow_user": "bili_users",
    "_get_up_latest_video": "bili_users",
    "search_bilibili_users": "bili_users",
    "get_up_info": "bili_users",
    "get_up_recent_videos": "bili_users",
    "get_following_live": "bili_users",
    "_oid_to_bvid": "bili_videos",
    "_get_video_info": "bili_videos",
    "_get_video_subtitles": "bili_videos",
    "_get_video_tags": "bili_videos",
    "_get_video_oid": "bili_videos",
    "_like_video": "bili_videos",
    "_coin_video": "bili_videos",
    "_fav_video": "bili_videos",
    "search_bilibili_videos": "bili_videos",
    "_upload_image_to_bilibili": "bili_dynamics",
    "_post_dynamic_text": "bili_dynamics",
    "_post_dynamic_with_image": "bili_dynamics",
    "_repost_video": "bili_dynamics",
    "get_up_recent_dynamics": "bili_dynamics",
    "get_following_updates": "bili_dynamics",
}


def bili_parts(package="core"):
    def cls(module, name):
        # 第一次用到时才导入：有的测试在单独的包名下加载插件代码，配置里没有 B站接口要的常量，也用不到它们
        def load():
            return getattr(import_module(f"{package}.infra.bili.{module}"), name)
        load.lazy = True
        return load

    class BiliParts(base_parts(package)):
        bili_auth = preferring(cls("auth", "BiliAuth"))
        bili_wbi = preferring(cls("wbi", "BiliWbi"))
        bili_comments = preferring(cls("comments", "BiliComments"))
        bili_messages = preferring(cls("messages", "BiliMessages"))
        bili_users = preferring(cls("users", "BiliUsers"))
        bili_videos = preferring(cls("videos", "BiliVideos"))
        bili_dynamics = preferring(cls("dynamics", "BiliDynamics"))

    return BiliParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "BiliParts":
        return bili_parts()
    raise AttributeError(name)


def attach_bili_groups(fake):
    """``SimpleNamespace`` 做的假插件：把直接挂在上面的旧名字（``_like_video`` 这些）分到对应的组上。

    七组都会挂上（没有旧名字的是空的，测试可以再往上放假方法）；组里的就是同一个对象，
    测试照旧在 ``fake.bili_videos._like_video`` 上断言。返回 ``fake`` 方便链式写。
    """
    from types import SimpleNamespace
    for owner in set(OWNERS.values()):
        if owner not in vars(fake):
            setattr(fake, owner, SimpleNamespace())
    for name, owner in OWNERS.items():
        if name in vars(fake):
            setattr(getattr(fake, owner), name, vars(fake)[name])
    return attach_base(fake)
