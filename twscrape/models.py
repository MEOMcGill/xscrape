import email.utils
import json
import os
import random
import re
import string
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Generator, Optional, Union

from .http import Response
from .logger import logger
from .utils import find_item, get_or, int_or, to_old_rep, utc

_KEEP_RAW = os.getenv("XSCRAPE_KEEP_RAW", "").lower() in ("1", "true", "yes")


@dataclass
class JSONTrait:
    def __post_init__(self):
        # Use object.__setattr__ so this survives even if a subclass sets
        # frozen=True in the future. `_extras` holds top-level keys present
        # in the parsed API response that the parser didn't explicitly
        # consume; `_raw` holds the full input dict when XSCRAPE_KEEP_RAW=1.
        if not hasattr(self, "_extras"):
            object.__setattr__(self, "_extras", {})

    @property
    def extras(self) -> dict:
        return getattr(self, "_extras", {}) or {}

    def dict(self):
        d = asdict(self)
        if self.extras:
            d["extras"] = dict(self.extras)
        raw = getattr(self, "_raw", None)
        if raw is not None:
            d["_raw"] = raw
        return d

    def json(self):
        return json.dumps(self.dict(), default=str)


@dataclass
class Coordinates(JSONTrait):
    longitude: float
    latitude: float

    # parse() reads from a Tweet-shaped obj; extras are computed against the
    # coordinates/geo sub-dict only, not the parent Tweet (Tweet handles its own).
    # Note: no type annotation → Python treats this as a class attribute, not a
    # dataclass field, so it doesn't end up in asdict() or __init__.
    _KNOWN_KEYS = frozenset({"coordinates", "type"})

    @staticmethod
    def parse(tw_obj: dict):
        if tw_obj.get("coordinates"):
            coords = tw_obj["coordinates"]["coordinates"]
            inst = Coordinates(coords[0], coords[1])
            _capture_extras(inst, tw_obj["coordinates"], Coordinates._KNOWN_KEYS)
            return inst
        if tw_obj.get("geo"):
            coords = tw_obj["geo"]["coordinates"]
            inst = Coordinates(coords[1], coords[0])
            _capture_extras(inst, tw_obj["geo"], Coordinates._KNOWN_KEYS)
            return inst
        return None


@dataclass
class Place(JSONTrait):
    id: str
    fullName: str
    name: str
    type: str
    country: str
    countryCode: str

    _KNOWN_KEYS = frozenset({"id", "full_name", "name", "place_type", "country", "country_code"})

    @staticmethod
    def parse(obj: dict):
        inst = Place(
            id=obj["id"],
            fullName=obj["full_name"],
            name=obj["name"],
            type=obj["place_type"],
            country=obj["country"],
            countryCode=obj["country_code"],
        )
        _capture_extras(inst, obj, Place._KNOWN_KEYS)
        return inst


@dataclass
class TextLink(JSONTrait):
    url: str
    text: str | None
    tcourl: str | None

    _KNOWN_KEYS = frozenset({"expanded_url", "url", "display_url", "indices"})

    @staticmethod
    def parse(obj: dict):
        url1 = obj.get("expanded_url")
        url2 = obj.get("url")
        text = obj.get("display_url")

        if not isinstance(url1, str) or not isinstance(url2, str):
            return None

        inst = TextLink(url=url1, text=text, tcourl=url2)
        _capture_extras(inst, obj, TextLink._KNOWN_KEYS)
        return inst


@dataclass
class AccountAbout(JSONTrait):
    screen_name: str
    name: str
    rest_id: int
    account_based_in: str | None
    location_accurate: bool | None
    affiliate_username: str | None
    source: str | None
    username_changes: int | None
    username_last_changed_at: int | None
    is_identity_verified: bool | None
    verified_since_msec: int | None

    _KNOWN_KEYS = frozenset({
        "about_profile", "core", "verification_info", "rest_id",
        # fallbacks read from top-level via get_required:
        "screen_name", "name",
        # structural keys present after to_old_obj flattening:
        "id", "id_str", "legacy",
    })

    @staticmethod
    def parse(obj: dict):
        about = obj.get("about_profile") or {}
        core = obj.get("core") or {}
        username_changes = about.get("username_changes", {}).get("count")
        username_last_changed = about.get("username_changes", {}).get("last_changed_at_msec")
        verification = obj.get("verification_info", {}) or {}
        reason = verification.get("reason", {}) or {}
        verified_since = reason.get("verified_since_msec")

        # Required fields
        screen_name = get_required(obj, core, "screen_name")
        name = get_required(obj, core, "name")
        rest_id_val = int_or(obj, "rest_id")
        if rest_id_val is None:
            raise KeyError(f"Required field 'rest_id' not found for item {screen_name}")

        inst = AccountAbout(
            screen_name=screen_name,
            name=name,
            rest_id=rest_id_val,
            account_based_in=about.get("account_based_in"),
            location_accurate=about.get("location_accurate"),
            affiliate_username=about.get("affiliate_username"),
            source=about.get("source"),
            username_changes=int(username_changes) if username_changes is not None else None,
            username_last_changed_at=int(username_last_changed)
            if username_last_changed is not None
            else None,
            is_identity_verified=verification.get("is_identity_verified"),
            verified_since_msec=int(verified_since) if verified_since is not None else None,
        )
        _capture_extras(inst, obj, AccountAbout._KNOWN_KEYS)
        return inst

@dataclass
class CommunityRule(JSONTrait):
    id_str: str
    name: str
    description: str

    @staticmethod
    def parse(obj: dict):
        return CommunityRule(
            id_str=str(obj.get("rest_id", obj.get("id_str", ""))),
            name=obj.get("name", ""),
            description=obj.get("description", ""),
        )


@dataclass
class Community(JSONTrait):
    id: int
    id_str: str
    name: str
    description: str | None
    memberCount: int
    moderatorCount: int
    rules: list[CommunityRule]
    topicId: str | None = None
    topicName: str | None = None
    isNsfw: bool | None = None

    @staticmethod
    def parse(obj: dict):
        id_str = str(obj.get("rest_id") or obj.get("id_str") or "")
        topic = obj.get("primary_community_topic") or {}
        rules = [CommunityRule.parse(x) for x in obj.get("rules", [])]
        return Community(
            id=int(id_str),
            id_str=id_str,
            name=obj.get("name", ""),
            description=obj.get("description"),
            memberCount=obj.get("member_count", 0),
            moderatorCount=obj.get("moderator_count", 0),
            rules=rules,
            topicId=topic.get("topic_id"),
            topicName=topic.get("topic_name"),
            isNsfw=obj.get("is_nsfw"),
        )




@dataclass
class UserRef(JSONTrait):
    id: int
    id_str: str
    username: str
    displayname: str
    _type: str = "snscrape.modules.twitter.UserRef"

    _KNOWN_KEYS = frozenset({"id_str", "core", "screen_name", "name", "id"})

    @staticmethod
    def parse(obj: dict):
        # Handle new nested structure where fields may be in 'core'
        core = obj.get("core") or {}
        screen_name = get_required(obj, core, "screen_name")
        name = get_required(obj, core, "name")

        inst = UserRef(
            id=int(obj["id_str"]),
            id_str=obj["id_str"],
            username=screen_name,
            displayname=name,
        )
        _capture_extras(inst, obj, UserRef._KNOWN_KEYS)
        return inst


@dataclass
class SuspendedUser(JSONTrait):
    id: int
    id_str: str
    message: str
    reason: str
    _type: str = "SuspendedUser"

    _KNOWN_KEYS = frozenset({"id_str", "message", "reason", "__typename", "id"})

    @staticmethod
    def parse(obj: dict, res=None):
        inst = SuspendedUser(
            id=int(obj["id_str"]),
            id_str=obj["id_str"],
            message=obj.get("message", ""),
            reason=obj.get("reason", ""),
        )
        _capture_extras(inst, obj, SuspendedUser._KNOWN_KEYS)
        return inst


@dataclass
class User(JSONTrait):
    id: int
    id_str: str
    url: str
    username: str
    displayname: str
    rawDescription: str
    created: datetime
    followersCount: int
    friendsCount: int
    statusesCount: int
    favouritesCount: int
    listedCount: int
    mediaCount: int
    location: str
    profileImageUrl: str
    profileBannerUrl: str | None = None
    protected: bool | None = None
    verified: bool | None = None
    blue: bool | None = None
    blueType: str | None = None
    descriptionLinks: list[TextLink] = field(default_factory=list)
    pinnedIds: list[int] = field(default_factory=list)
    _type: str = "snscrape.modules.twitter.User"

    # todo:
    # link: typing.Optional[TextLink] = None
    # label: typing.Optional["UserLabel"] = None

    _KNOWN_KEYS = frozenset({
        # structural / identity
        "__typename", "id", "id_str", "rest_id", "core", "legacy",
        # legacy fields flattened onto top-level that parse() reads
        "screen_name", "name", "created_at", "description",
        "followers_count", "friends_count", "statuses_count",
        "favourites_count", "listed_count", "media_count",
        "location", "profile_image_url_https",
        "profile_banner_url", "verified", "protected",
        "entities", "pinned_tweet_ids_str",
        # top-level fields parse() reads
        "is_blue_verified", "verified_type",
        # known top-level fields currently sent by X but not (yet) modeled.
        # Captured here so we don't log them on every run; promote to actual
        # User fields when you want to expose them. Raise novelty when X adds
        # something outside this set.
        "affiliates_highlighted_label", "business_account", "can_dm", "can_media_tag",
        "creator_subscriptions_count", "default_profile", "default_profile_image",
        "fast_followers_count", "following", "has_custom_timelines",
        "has_graduated_access", "has_hidden_subscriptions_on_profile",
        "has_nft_avatar", "highlights_info", "is_profile_translatable",
        "is_translator", "legacy_extended_profile", "normal_followers_count",
        "possibly_sensitive", "professional", "profile_image_shape",
        "profile_interstitial_type", "super_follow_eligible",
        "tipjar_settings", "translator_type", "url",
        "verification_info", "want_retweets", "withheld_in_countries",
    })

    @staticmethod
    def parse(obj: dict, res=None):
        # obj is already flattened by _flatten_user_v2 — the single source of truth
        # for X's response shape (core/legacy/avatar/verification/privacy/profile_bio
        # are merged onto the top level there). Read flat keys directly.
        #
        # screen_name/name are truly required: a user without them is unusable, so
        # raise (surfaced as a parse dump) rather than emit a junk record.
        screen_name = obj.get("screen_name")
        name = obj.get("name")
        if not screen_name or not name:
            raise KeyError(
                f"User missing required field screen_name/name (id={obj.get('id_str', 'unknown')})"
            )

        # created_at is absent on partial user payloads (e.g. community members);
        # fall back to epoch so downstream date parsing stays graceful.
        created_at = obj.get("created_at") or "Thu Jan 01 00:00:00 +0000 1970"
        entities = obj.get("entities") or {}
        pinned_ids = obj.get("pinned_tweet_ids_str") or []

        inst = User(
            id=int(obj["id_str"]),
            id_str=obj["id_str"],
            url=f"https://x.com/{screen_name}",
            username=screen_name,
            displayname=name,
            rawDescription=obj.get("description", ""),
            created=email.utils.parsedate_to_datetime(created_at),
            followersCount=obj.get("followers_count", 0),
            friendsCount=obj.get("friends_count", 0),
            statusesCount=obj.get("statuses_count", 0),
            favouritesCount=obj.get("favourites_count", 0),
            listedCount=obj.get("listed_count", 0),
            mediaCount=obj.get("media_count", 0),
            location=obj.get("location", ""),
            profileImageUrl=obj.get("profile_image_url_https", ""),
            profileBannerUrl=obj.get("profile_banner_url"),
            verified=obj.get("verified"),
            blue=obj.get("is_blue_verified"),
            blueType=obj.get("verified_type"),
            protected=obj.get("protected"),
            descriptionLinks=_parse_links(
                {"entities": entities}, ["entities.description.urls", "entities.url.urls"]
            ),
            pinnedIds=[int(x) for x in pinned_ids],
        )
        _capture_extras(inst, obj, User._KNOWN_KEYS)
        return inst


@dataclass
class Tweet(JSONTrait):
    id: int
    id_str: str
    url: str
    date: datetime
    user: User
    lang: str
    rawContent: str
    replyCount: int
    retweetCount: int
    likeCount: int
    quoteCount: int
    bookmarkedCount: int
    conversationId: int
    conversationIdStr: str
    hashtags: list[str]
    cashtags: list[str]
    mentionedUsers: list[UserRef]
    links: list[TextLink]
    media: "Media"
    viewCount: int | None = None
    retweetedTweet: Optional["Tweet"] = None
    quotedTweet: Optional["Tweet"] = None
    place: Place | None = None
    coordinates: Coordinates | None = None
    inReplyToTweetId: int | None = None
    inReplyToTweetIdStr: str | None = None
    inReplyToUser: UserRef | None = None
    source: str | None = None
    sourceUrl: str | None = None
    sourceLabel: str | None = None
    card: Union[None, "SummaryCard", "PollCard", "BroadcastCard", "AudiospaceCard"] = None
    possibly_sensitive: bool | None = None
    isQuoteStatus: bool = False
    isTranslatable: bool = False
    displayTextRange: list[int] | None = None
    inReplyToScreenName: str | None = None
    editControl: dict | None = None
    voiceInfo: dict | None = None
    _type: str = "snscrape.modules.twitter.Tweet"

    # todo:
    # renderedContent: str
    # vibe: Optional["Vibe"] = None

    _KNOWN_KEYS = frozenset({
        # structural / identity
        "id", "id_str", "rest_id", "core", "legacy", "__typename",
        # top-level fields parse() reads directly
        "user_id_str", "created_at", "lang", "full_text",
        "reply_count", "retweet_count", "favorite_count", "quote_count",
        "bookmark_count", "conversation_id_str",
        "entities", "extended_entities", "note_tweet",
        "place", "coordinates", "geo",
        "in_reply_to_status_id_str", "in_reply_to_user_id_str",
        "in_reply_to_screen_name",
        "source", "possibly_sensitive", "card",
        "views", "ext_views",
        "retweeted_status_id_str", "retweeted_status_result",
        "quoted_status_id_str", "quoted_status_result",
        # known top-level fields X currently sends but parser doesn't consume
        # — silenced to keep steady-state logs clean; promote to Tweet fields
        # when you want to expose them.
        "article", "birdwatch_pivot", "bookmarked", "conversation_control",
        "display_text_range", "edit_control", "edit_perspective", "favorited",
        "has_birdwatch_notes", "is_quote_status", "is_translatable",
        "limited_actions", "possibly_sensitive_editable", "previous_counts",
        "quick_promote_eligibility", "quoted_status_permalink", "quotedRefResult",
        "retweeted", "scopes", "unmention_data",
    })

    @staticmethod
    def parse(obj: dict, res: dict):
        tw_usr = User.parse(res["users"][obj["user_id_str"]])

        rt_id_path = [
            "retweeted_status_id_str",
            "retweeted_status_result.result.rest_id",
            "retweeted_status_result.result.tweet.rest_id",
        ]

        qt_id_path = [
            "quoted_status_id_str",
            "quoted_status_result.result.rest_id",
            "quoted_status_result.result.tweet.rest_id",
        ]

        rt_obj = get_or(res, f"tweets.{_first(obj, rt_id_path)}")
        qt_obj = get_or(res, f"tweets.{_first(obj, qt_id_path)}")

        url = f"https://x.com/{tw_usr.username}/status/{obj['id_str']}"
        doc = Tweet(
            id=int(obj["id_str"]),
            id_str=obj["id_str"],
            url=url,
            date=email.utils.parsedate_to_datetime(obj["created_at"]),
            user=tw_usr,
            lang=obj["lang"],
            rawContent=get_or(obj, "note_tweet.note_tweet_results.result.text", obj["full_text"]),
            replyCount=obj["reply_count"],
            retweetCount=obj["retweet_count"],
            likeCount=obj["favorite_count"],
            quoteCount=obj["quote_count"],
            bookmarkedCount=get_or(obj, "bookmark_count", 0),
            conversationId=int(obj["conversation_id_str"]),
            conversationIdStr=obj["conversation_id_str"],
            hashtags=[x["text"] for x in get_or(obj, "entities.hashtags", [])],
            cashtags=[x["text"] for x in get_or(obj, "entities.symbols", [])],
            mentionedUsers=[UserRef.parse(x) for x in get_or(obj, "entities.user_mentions", [])],
            links=_parse_links(
                obj, ["entities.urls", "note_tweet.note_tweet_results.result.entity_set.urls"]
            ),
            viewCount=_get_views(obj, rt_obj or {}),
            retweetedTweet=Tweet.parse(rt_obj, res) if rt_obj else None,
            quotedTweet=Tweet.parse(qt_obj, res) if qt_obj else None,
            place=Place.parse(obj["place"]) if obj.get("place") else None,
            coordinates=Coordinates.parse(obj),
            inReplyToTweetId=int_or(obj, "in_reply_to_status_id_str"),
            inReplyToTweetIdStr=get_or(obj, "in_reply_to_status_id_str"),
            inReplyToUser=_get_reply_user(obj, res),
            source=obj.get("source"),
            sourceUrl=_get_source_url(obj),
            sourceLabel=_get_source_label(obj),
            media=Media.parse(obj),
            card=_parse_card(obj, url),
            possibly_sensitive=obj.get("possibly_sensitive"),
            isQuoteStatus=obj.get("is_quote_status", False),
            isTranslatable=obj.get("is_translatable", False),
            displayTextRange=obj.get("display_text_range"),
            inReplyToScreenName=obj.get("in_reply_to_screen_name"),
            editControl=_parse_edit_control(obj),
            voiceInfo=obj.get("voice_info"),
        )

        # issue #42 – restore full rt text
        rt = doc.retweetedTweet
        if rt is not None and rt.user is not None and doc.rawContent.endswith("…"):
            rt_msg = f"RT @{rt.user.username}: {rt.rawContent}"
            if doc.rawContent != rt_msg:
                doc.rawContent = rt_msg

        _capture_extras(doc, obj, Tweet._KNOWN_KEYS)
        return doc


@dataclass
class MediaPhoto(JSONTrait):
    url: str

    _KNOWN_KEYS = frozenset({
        "media_url_https", "type", "id", "id_str", "display_url", "expanded_url",
        "indices", "url", "features", "sizes", "original_info",
        "media_key", "ext_media_availability", "ext_alt_text", "source_user_id",
        "source_user_id_str", "source_status_id", "source_status_id_str",
    })

    @staticmethod
    def parse(obj: dict):
        inst = MediaPhoto(url=obj["media_url_https"])
        _capture_extras(inst, obj, MediaPhoto._KNOWN_KEYS)
        return inst


@dataclass
class MediaVideo(JSONTrait):
    thumbnailUrl: str
    variants: list["MediaVideoVariant"]
    duration: int
    views: int | None = None

    _KNOWN_KEYS = frozenset({
        "media_url_https", "video_info", "mediaStats", "type",
        "id", "id_str", "display_url", "expanded_url", "indices", "url",
        "features", "sizes", "original_info", "media_key",
        "ext_media_availability", "additional_media_info",
        "source_user_id", "source_user_id_str", "source_status_id", "source_status_id_str",
    })

    @staticmethod
    def parse(obj: dict):
        inst = MediaVideo(
            thumbnailUrl=obj["media_url_https"],
            variants=[
                MediaVideoVariant.parse(x) for x in obj["video_info"]["variants"] if "bitrate" in x
            ],
            duration=obj["video_info"]["duration_millis"],
            views=int_or(obj, "mediaStats.viewCount"),
        )
        _capture_extras(inst, obj, MediaVideo._KNOWN_KEYS)
        return inst


@dataclass
class MediaAnimated(JSONTrait):
    thumbnailUrl: str
    videoUrl: str

    _KNOWN_KEYS = frozenset({
        "media_url_https", "video_info", "type",
        "id", "id_str", "display_url", "expanded_url", "indices", "url",
        "features", "sizes", "original_info", "media_key",
        "ext_media_availability",
    })

    @staticmethod
    def parse(obj: dict):
        try:
            inst = MediaAnimated(
                thumbnailUrl=obj["media_url_https"],
                videoUrl=obj["video_info"]["variants"][0]["url"],
            )
        except KeyError:
            return None
        _capture_extras(inst, obj, MediaAnimated._KNOWN_KEYS)
        return inst


@dataclass
class MediaVideoVariant(JSONTrait):
    contentType: str
    bitrate: int
    url: str

    _KNOWN_KEYS = frozenset({"content_type", "bitrate", "url"})

    @staticmethod
    def parse(obj: dict):
        inst = MediaVideoVariant(
            contentType=obj["content_type"],
            bitrate=obj["bitrate"],
            url=obj["url"],
        )
        _capture_extras(inst, obj, MediaVideoVariant._KNOWN_KEYS)
        return inst


@dataclass
class Media(JSONTrait):
    photos: list[MediaPhoto] = field(default_factory=list)
    videos: list[MediaVideo] = field(default_factory=list)
    animated: list[MediaAnimated] = field(default_factory=list)

    # Media.parse takes a Tweet-shaped obj and reads obj["extended_entities"].
    # Not wired to _capture_extras — Tweet handles top-level drift; Media only
    # aggregates pre-parsed sub-entities (photos/videos/animated).

    @staticmethod
    def parse(obj: dict):
        photos: list[MediaPhoto] = []
        videos: list[MediaVideo] = []
        animated: list[MediaAnimated] = []

        for x in get_or(obj, "extended_entities.media", []):
            if x["type"] == "video":
                if video := MediaVideo.parse(x):
                    videos.append(video)
                continue

            if x["type"] == "photo":
                if photo := MediaPhoto.parse(x):
                    photos.append(photo)
                continue

            if x["type"] == "animated_gif":
                if animated_gif := MediaAnimated.parse(x):
                    animated.append(animated_gif)
                continue

            logger.warning(f"Unknown media type: {x['type']}: {json.dumps(x)}")

        return Media(photos=photos, videos=videos, animated=animated)


@dataclass
class Card(JSONTrait):
    pass


@dataclass
class SummaryCard(Card):
    title: str
    description: str
    vanityUrl: str
    url: str
    photo: MediaPhoto | None = None
    video: MediaVideo | None = None
    _type: str = "summary"


@dataclass
class PollOption(JSONTrait):
    label: str
    votesCount: int


@dataclass
class PollCard(Card):
    options: list[PollOption]
    finished: bool
    _type: str = "poll"


@dataclass
class BroadcastCard(Card):
    title: str
    url: str
    photo: MediaPhoto | None = None
    _type: str = "broadcast"


@dataclass
class AudiospaceCard(Card):
    url: str
    _type: str = "audiospace"


@dataclass
class RequestParam(JSONTrait):
    key: str
    value: str


@dataclass
class TrendUrl(JSONTrait):
    url: str
    urlType: str
    urlEndpointOptions: list[RequestParam]

    _KNOWN_KEYS = frozenset({"url", "urlType", "urtEndpointOptions"})

    @staticmethod
    def parse(obj: dict):
        urt = obj.get("urtEndpointOptions") or {}
        inst = TrendUrl(
            url=obj["url"],
            urlType=obj["urlType"],
            urlEndpointOptions=[
                RequestParam(key=x["key"], value=x["value"])
                for x in urt.get("requestParams", [])
            ],
        )
        _capture_extras(inst, obj, TrendUrl._KNOWN_KEYS)
        return inst


@dataclass
class TrendMetadata(JSONTrait):
    domain_context: str | None
    meta_description: str | None
    url: TrendUrl

    _KNOWN_KEYS = frozenset({"domain_context", "meta_description", "url"})

    @staticmethod
    def parse(obj: dict):
        inst = TrendMetadata(
            domain_context=obj.get("domain_context"),
            meta_description=obj.get("meta_description"),
            url=TrendUrl.parse(obj["url"]),
        )
        _capture_extras(inst, obj, TrendMetadata._KNOWN_KEYS)
        return inst


@dataclass
class GroupedTrend(JSONTrait):
    name: str
    url: TrendUrl

    _KNOWN_KEYS = frozenset({"name", "url"})

    @staticmethod
    def parse(obj: dict):
        inst = GroupedTrend(name=obj["name"], url=TrendUrl.parse(obj["url"]))
        _capture_extras(inst, obj, GroupedTrend._KNOWN_KEYS)
        return inst


@dataclass
class Trend(JSONTrait):
    id: str | None
    rank: str | int | None
    name: str
    trend_url: TrendUrl
    trend_metadata: TrendMetadata
    grouped_trends: list[GroupedTrend] = field(default_factory=list)
    _type: str = "timelinetrend"

    _KNOWN_KEYS = frozenset({"name", "rank", "trend_url", "trend_metadata", "grouped_trends"})

    @staticmethod
    def parse(obj: dict, res=None):
        grouped_trends = [GroupedTrend.parse(x) for x in obj.get("grouped_trends", [])]
        inst = Trend(
            id=f"trend-{obj['name']}",
            name=obj["name"],
            rank=int(obj["rank"]) if "rank" in obj else None,
            trend_url=TrendUrl.parse(obj["trend_url"]),
            trend_metadata=TrendMetadata.parse(obj["trend_metadata"]),
            grouped_trends=grouped_trends,
        )
        _capture_extras(inst, obj, Trend._KNOWN_KEYS)
        return inst


def _parse_card_get_bool(values: list[dict], key: str):
    for x in values:
        if x["key"] == key:
            return x["value"]["boolean_value"]
    return False


def _parse_card_get_str(values: list[dict], key: str, defaultVal=None) -> str | None:
    for x in values:
        if x["key"] == key:
            return x["value"]["string_value"]
    return defaultVal


def _parse_card_extract_str(values: list[dict], key: str):
    pretenders = [x["value"]["string_value"] for x in values if x["key"] == key]
    new_values = [x for x in values if x["key"] != key]
    return pretenders[0] if pretenders else "", new_values


def _parse_card_extract_title(values: list[dict]):
    new_values, pretenders = [], []
    # title is trimmed to 70 chars, so try to find the longest text in alt_text
    for x in values:
        k = x["key"]
        if k == "title" or k.endswith("_alt_text"):
            pretenders.append(x["value"]["string_value"])
        else:
            new_values.append(x)

    pretenders = sorted(pretenders, key=lambda x: len(x), reverse=True)
    return pretenders[0] if pretenders else "", new_values


def _parse_card_extract_largest_photo(values: list[dict]):
    photos = [x for x in values if x["value"]["type"] == "IMAGE"]
    photos = sorted(photos, key=lambda x: x["value"]["image_value"]["height"], reverse=True)
    values = [x for x in values if x["value"]["type"] != "IMAGE"]
    if photos:
        return MediaPhoto(url=photos[0]["value"]["image_value"]["url"]), values
    else:
        return None, values


def _parse_card_prepare_values(obj: dict):
    values = get_or(obj, "card.legacy.binding_values", [])
    # values = sorted(values, key=lambda x: x["key"])
    # values = [x for x in values if x["key"] not in {"domain", "creator", "site"}]
    values = [x for x in values if x["value"]["type"] != "IMAGE_COLOR"]
    return values


def _parse_card(obj: dict, url: str):
    name = get_or(obj, "card.legacy.name", None)
    if not name:
        return None

    if name in {"summary", "summary_large_image", "player"}:
        val = _parse_card_prepare_values(obj)
        title, val = _parse_card_extract_title(val)
        description, val = _parse_card_extract_str(val, "description")
        vanity_url, val = _parse_card_extract_str(val, "vanity_url")
        url, val = _parse_card_extract_str(val, "card_url")
        photo, val = _parse_card_extract_largest_photo(val)

        return SummaryCard(
            title=title,
            description=description,
            vanityUrl=vanity_url,
            url=url,
            photo=photo,
        )

    if name == "unified_card":
        val = _parse_card_prepare_values(obj)
        val = [x for x in val if x["key"] == "unified_card"][0]["value"]["string_value"]
        val = json.loads(val)

        co = get_or(val, "component_objects", {})
        do = get_or(val, "destination_objects", {})
        me = list(get_or(val, "media_entities", {}).values())
        if len(me) > 1:
            logger.debug(f"[Card] Multiple media entities: {json.dumps(me, indent=2)}")

        me = me[0] if me else {}

        title = get_or(co, "details_1.data.title.content", "")
        description = get_or(co, "details_1.data.subtitle.content", "")
        vanity_url = get_or(do, "browser_with_docked_media_1.data.url_data.vanity", "")
        url = get_or(do, "browser_with_docked_media_1.data.url_data.url", "")
        video = MediaVideo.parse(me) if me and me["type"] == "video" else None
        photo = MediaPhoto.parse(me) if me and me["type"] == "photo" else None

        return SummaryCard(
            title=title,
            description=description,
            vanityUrl=vanity_url,
            url=url,
            photo=photo,
            video=video,
        )

    if re.match(r"poll\d+choice_text_only", name):
        val = _parse_card_prepare_values(obj)

        options = []
        for x in range(20):
            label = _parse_card_get_str(val, f"choice{x + 1}_label")
            votes = _parse_card_get_str(val, f"choice{x + 1}_count")
            if label is None or votes is None:
                break

            options.append(PollOption(label=label, votesCount=int(votes)))

        finished = _parse_card_get_bool(val, "counts_are_final")
        # duration_minutes = int(_parse_card_get_str(val, "duration_minutes") or "0")
        # end_datetime_utc = _parse_card_get_str(val, "end_datetime_utc")
        # print(json.dumps(val, indent=2))
        return PollCard(options=options, finished=finished)

    if name == "745291183405076480:broadcast":
        val = _parse_card_prepare_values(obj)
        card_url = _parse_card_get_str(val, "broadcast_url")
        card_title = _parse_card_get_str(val, "broadcast_title")
        photo, _ = _parse_card_extract_largest_photo(val)
        if card_url is None or card_title is None:
            return None

        return BroadcastCard(title=card_title, url=card_url, photo=photo)

    if name == "3691233323:audiospace":
        # no more data in this object, possible extra api call needed to get card info
        val = _parse_card_prepare_values(obj)
        card_url = _parse_card_get_str(val, "card_url")
        if card_url is None:
            return None

        # print(json.dumps(val, indent=2))
        return AudiospaceCard(url=card_url)

    logger.warning(f"Unknown card type '{name}' on {url}")
    if "PYTEST_CURRENT_TEST" in os.environ:  # help debugging tests
        print(f"Unknown card type '{name}' on {url}", file=sys.stderr)
        # print(json.dumps(obj["card"]["legacy"], indent=2))
    return None


# internal helpers


def _parse_edit_control(obj: dict):
    edit = obj.get("edit_control")
    if not isinstance(edit, dict):
        return None

    initial = edit.get("edit_control_initial")
    if not isinstance(initial, dict):
        return edit

    return {**initial, **{k: v for k, v in edit.items() if k != "edit_control_initial"}}


def _get_reply_user(tw_obj: dict, res: dict):
    user_id = tw_obj.get("in_reply_to_user_id_str")
    if user_id is None:
        return None

    if user_id in res["users"]:
        return UserRef.parse(res["users"][user_id])

    mentions = get_or(tw_obj, "entities.user_mentions", [])
    mention = find_item(mentions, lambda x: x["id_str"] == tw_obj["in_reply_to_user_id_str"])
    if mention:
        return UserRef.parse(mention)

    # todo: user not found in reply (probably deleted or hidden)
    return None


def _get_source_url(tw_obj: dict):
    source = tw_obj.get("source")
    if source and (match := re.search(r'href=[\'"]?([^\'" >]+)', source)):
        return str(match.group(1))
    return None


def _get_source_label(tw_obj: dict):
    source = tw_obj.get("source")
    if source and (match := re.search(r">([^<]*)<", source)):
        return str(match.group(1))
    return None


def _parse_links(obj: dict, paths: list[str]):
    links = []
    for x in paths:
        links.extend(get_or(obj, x, []))

    links = [TextLink.parse(x) for x in links]
    links = [x for x in links if x is not None]

    return links


def get_required(obj: dict, core: dict, key: str):
    """Get value from core or obj, raise KeyError if missing in both"""
    value = core.get(key) or obj.get(key)
    if value is None:
        raise KeyError(
            f"Required field '{key}' not found in core or obj for item {obj.get('id_str', 'unknown')}"
        )
    return value


# Deduplicated log state: emit one INFO line per (model-class, unknown-key) per process.
_SEEN_EXTRAS: set[tuple[str, str]] = set()


def _capture_extras(model, obj: dict, known) -> None:
    """Stash every top-level key in `obj` that's not in `known` onto `model._extras`.

    Called at the end of each model's `parse()` so that when X ships a new field
    we neither drop it silently nor crash. See JSONTrait.extras / .dict() for how
    the captured values surface to downstream consumers.
    """
    if not isinstance(obj, dict):
        return
    extra = {k: v for k, v in obj.items() if k not in known}
    if _KEEP_RAW:
        object.__setattr__(model, "_raw", obj)
    if not extra:
        return
    object.__setattr__(model, "_extras", extra)
    cls_name = type(model).__name__
    for k in extra:
        seen_key = (cls_name, k)
        if seen_key not in _SEEN_EXTRAS:
            _SEEN_EXTRAS.add(seen_key)
            logger.info(f"xscrape: unknown top-level key on {cls_name}: {k!r}")


def _first(obj: dict, paths: list[str]):
    for x in paths:
        cid = get_or(obj, x, None)
        if cid is not None:
            return cid
    return None


def _get_views(obj: dict, rt_obj: dict):
    for x in [obj, rt_obj]:
        for y in ["ext_views.count", "views.count"]:
            k = int_or(x, y)
            if k is not None:
                return k
    return None


def _write_dump(kind: str, e: Exception, x: dict, obj: dict):
    uniq = "".join(random.choice(string.ascii_lowercase) for _ in range(5))
    time = utc.now().strftime("%Y-%m-%d_%H-%M-%S")
    dumpfile = f"/tmp/twscrape/twscrape_parse_error_{time}_{uniq}.txt"
    os.makedirs(os.path.dirname(dumpfile), exist_ok=True)

    with open(dumpfile, "w") as fp:
        msg = [
            f"Error parsing {kind}. Error: {type(e)}",
            traceback.format_exc(),
            json.dumps(x, default=str),
            json.dumps(obj, default=str),
        ]
        fp.write("\n\n".join(msg))

    logger.error(f"Failed to parse response of {kind}, writing dump to {dumpfile}")


def _parse_items(rep: Response, kind: str, limit: int = -1):
    if kind == "user":
        Cls, key = User, "users"
    elif kind == "tweet":
        Cls, key = Tweet, "tweets"
    elif kind == "trends":
        Cls, key = Trend, "trends"
    else:
        raise ValueError(f"Invalid kind: {kind}")

    # check for dict, because Response can be mocked in tests with different type
    res = rep if isinstance(rep, dict) else rep.json()
    obj = to_old_rep(res)

    ids = set()
    for x in obj[key].values():
        if limit != -1 and len(ids) >= limit:
            # todo: move somewhere in configuration like force_limit
            # https://github.com/vladkens/twscrape/issues/26#issuecomment-1656875132
            # break
            pass

        try:
            if kind == "user" and x.get("__typename") == "UserUnavailable":
                tmp = SuspendedUser.parse(x, obj)
            else:
                tmp = Cls.parse(x, obj)
            if tmp.id not in ids:
                ids.add(tmp.id)
                yield tmp
        except Exception as e:
            _write_dump(kind, e, x, obj)
            continue


# public helpers


def parse_tweet(rep: Response, twid: int) -> Tweet | None:
    try:
        docs = list(parse_tweets(rep))
        for x in docs:
            if x.id == twid:
                return x
        return None
    except Exception as e:
        logger.error(f"Failed to parse tweet {twid} - {type(e)}:\n{traceback.format_exc()}")
        return None


def parse_user(rep: Response) -> User | None:
    try:
        docs = list(parse_users(rep))
        if len(docs) == 1:
            return docs[0]
        return None
    except Exception as e:
        logger.error(f"Failed to parse user - {type(e)}:\n{traceback.format_exc()}")
        return None


def parse_trend(rep: Response) -> Trend | None:
    try:
        docs = list(parse_trends(rep))
        if len(docs) == 1:
            return docs[0]
        return None
    except Exception as e:
        logger.error(f"Failed to parse trend - {type(e)}:\n{traceback.format_exc()}")
        return None


def parse_about(rep: Response | dict) -> AccountAbout | None:
    try:
        res = rep if isinstance(rep, dict) else rep.json()
        obj = get_or(res, "data.user_result_by_screen_name.result")
        if not obj:
            return None
        return AccountAbout.parse(obj)
    except Exception as e:
        logger.error(f"Failed to parse about profile - {type(e)}:\n{traceback.format_exc()}")
        return None

def parse_community(rep: Response | dict) -> Community | None:
    try:
        res = rep if isinstance(rep, dict) else rep.json()
        community = get_or(res, "data.communityResults.result")
        if not community:
            return None
        return Community.parse(community)
    except Exception as e:
        logger.error(f"Failed to parse community - {type(e)}:\n{traceback.format_exc()}")
        return None


def parse_tweets(rep: Response, limit: int = -1) -> Generator[Tweet, None, None]:
    return _parse_items(rep, "tweet", limit)  # type: ignore


def parse_users(rep: Response, limit: int = -1) -> Generator[User, None, None]:
    return _parse_items(rep, "user", limit)  # type: ignore


def parse_trends(rep: Response, limit: int = -1) -> Generator[Trend, None, None]:
    return _parse_items(rep, kind="trends", limit=limit)  # type: ignore
