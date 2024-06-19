from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes, ConversationHandler

from obeyd.config import REVIEW_JOKES_CHAT_ID, SCORES, VOICES_BASE_DIR
from obeyd.db import db
from obeyd.middlewares import authenticated, log_activity
from obeyd.thompson import ThompsonSampling


def format_text_joke(joke: dict):
    return f"{joke['text']}\n\n<b>{joke['creator_nickname']}</b>"


async def send_joke(
    joke: dict,
    chat_id: str | int,
    context: ContextTypes.DEFAULT_TYPE,
    kwargs: dict,
):
    if joke["kind"] == "text":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{format_text_joke(joke)}",
            **kwargs,
        )
    elif joke["kind"] == "voice":
        await context.bot.send_voice(
            chat_id=chat_id,
            voice=Path(f"{VOICES_BASE_DIR}/{joke['voice_file_id']}.bin"),
            caption=f"<b>{joke['creator_nickname']}</b>",
            **kwargs,
        )
    else:
        raise Exception("expected 'kind' to be one of 'text' or 'voice'")


def score_inline_keyboard_markup(joke: dict):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=score_data["emoji"],
                    callback_data=f"scorejoke:{str(joke['_id'])}:{score}",
                )
                for score, score_data in SCORES.items()
            ]
        ]
    )


async def send_joke_to_chat(
    joke: dict, chat_id: str | int, context: ContextTypes.DEFAULT_TYPE
):
    common = {
        "reply_markup": score_inline_keyboard_markup(joke),
    }

    await send_joke(joke, chat_id, context, common)


NEWJOKE_STATES_TEXT = 1


@log_activity("joke")
async def joke_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message
    assert update.effective_user
    assert update.effective_chat

    joke = await thompson_sampled_joke(for_user_id=update.effective_user.id)

    if joke is None:
        await update.message.reply_text(
            "دیگه جوکی ندارم که بهت بگم 😁 میتونی به جاش تو یک جوک بهم بگی",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/newjoke")]],
                one_time_keyboard=True,
                resize_keyboard=True,
            ),
        )
        return

    await db["joke_views"].insert_one(
        {
            "user_id": update.effective_user.id,
            "joke_id": joke["_id"],
            "score": None,
            "viewed_at": datetime.now(tz=timezone.utc),
            "scored_at": None,
        }
    )

    chat_id = update.effective_chat.id
    await send_joke_to_chat(joke, chat_id, context)

    return ConversationHandler.END


@authenticated
@log_activity("newjoke")
async def newjoke_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict
):
    assert update.message

    await update.message.reply_text(
        text="بگو 😁",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/cancel")]],
            one_time_keyboard=True,
            resize_keyboard=True,
        ),
    )

    return NEWJOKE_STATES_TEXT


@authenticated
async def newjoke_handler_joke(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict
):
    assert update.message
    assert update.effective_user
    assert context.job_queue

    if update.message.voice is not None:
        file = await update.message.voice.get_file()
        file_id = str(uuid4())
        await file.download_to_drive(custom_path=f"{VOICES_BASE_DIR}/{file_id}.bin")
        joke = {"kind": "voice", "voice_file_id": file_id}
    else:
        joke = {"kind": "text", "text": update.message.text}

    joke = {
        **joke,
        "creator_id": user["user_id"],
        "creator_nickname": user["nickname"],
        "created_at": datetime.now(tz=timezone.utc),
    }
    await db["jokes"].insert_one(joke)

    context.job_queue.run_once(
        callback=newjoke_callback_notify_admin,
        when=0,
        data=joke,
    )

    await update.message.reply_text(
        "😂👍",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/joke")],
                [KeyboardButton(text="/newjoke")],
            ],
            one_time_keyboard=True,
            resize_keyboard=True,
        ),
    )

    return ConversationHandler.END


def joke_review_inline_keyboard_markup(joke: dict):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="رد",
                    callback_data=f"reviewjoke:{joke['_id']}:reject",
                ),
                InlineKeyboardButton(
                    text="تایید",
                    callback_data=f"reviewjoke:{joke['_id']}:accept",
                ),
            ]
        ]
    )


async def send_joke_to_admin(joke: dict, context: ContextTypes.DEFAULT_TYPE):
    common = {
        "reply_markup": joke_review_inline_keyboard_markup(joke),
    }

    await send_joke(joke, REVIEW_JOKES_CHAT_ID, context, common)


async def newjoke_callback_notify_admin(context: ContextTypes.DEFAULT_TYPE):
    assert context.job
    assert isinstance(context.job.data, dict)

    joke = context.job.data

    await send_joke_to_admin(joke, context)


async def update_joke_sent_to_admin(joke: dict, update: Update, accepted: bool):
    assert update.callback_query
    assert update.effective_user

    info_msg = f"{'تایید' if accepted else 'رد'} شده توسط <b>{update.effective_user.full_name}</b>"

    if joke["kind"] == "text":
        await update.callback_query.edit_message_text(
            text=f"{format_text_joke(joke)}\n\n{info_msg}",
            reply_markup=joke_review_inline_keyboard_markup(joke),
        )
    elif joke["kind"] == "voice":
        await update.callback_query.edit_message_caption(
            caption=f"<b>{joke['creator_nickname']}</b>\n\n{info_msg}",
            reply_markup=joke_review_inline_keyboard_markup(joke),
        )
    else:
        raise Exception("expected 'kind' to be one of 'text' or 'voice'")


@log_activity("reviewjoke")
async def reviewjoke_callback_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    assert update.callback_query
    assert update.effective_user
    assert isinstance(update.callback_query.data, str)

    _, joke_id, action = tuple(update.callback_query.data.split(":"))
    accepted = None
    if action == "accept":
        accepted = True
    elif action == "reject":
        accepted = False
    else:
        raise Exception("expected accept or reject")

    await db["jokes"].update_one(
        {"_id": ObjectId(joke_id)}, {"$set": {"accepted": accepted}}
    )

    joke = await db["jokes"].find_one({"_id": ObjectId(joke_id)})
    assert joke is not None

    if accepted:
        await update.callback_query.answer("تایید شد")
    else:
        await update.callback_query.answer("رد شد")
    await update_joke_sent_to_admin(joke, update, accepted=accepted)


async def random_joke(constraints: list[dict] = []):
    try:
        return (
            await db["jokes"]
            .aggregate(
                [{"$match": {"accepted": True}}, {"$sample": {"size": 1}}, *constraints]
            )
            .next()
        )
    except StopAsyncIteration:
        return None


async def thompson_sampled_joke(for_user_id: int | None) -> dict | None:
    views = await db["joke_views"].find({"user_id": for_user_id}).to_list(None)

    pipeline = [
        {
            "$match": {
                "accepted": True,
                "_id": {"$nin": [view["joke_id"] for view in views]},
            }
        },
        {
            "$lookup": {
                "from": "joke_views",
                "localField": "_id",
                "foreignField": "joke_id",
                "as": "views",
            }
        },
    ]

    results = await db.jokes.aggregate(pipeline).to_list(None)

    if len(results) == 0:
        return None

    thompson = ThompsonSampling(n_arms=len(results), default_mean=3.0, default_var=2.0)

    average_user_score = {}
    for joke in results:
        for view in joke["views"]:
            if "score" not in view or view["score"] is None:
                continue
            if view["user_id"] not in average_user_score:
                average_user_score[view["user_id"]] = {"count": 0, "sum": 0}
            average_user_score[view["user_id"]]["count"] += 1
            average_user_score[view["user_id"]]["sum"] += view["score"]

    for i, joke in enumerate(results):
        for view in joke["views"]:
            score = None
            if "score" not in view or view["score"] is None:
                if view["user_id"] in average_user_score:
                    score = (
                        average_user_score[view["user_id"]]["sum"]
                        / average_user_score[view["user_id"]]["count"]
                    )
            else:
                score = view["score"]
            if score:
                thompson.insert_observation(i, score)

    selected_joke = results[int(thompson.select_arm())]

    return selected_joke


@log_activity("inlinequery")
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.inline_query

    joke = await thompson_sampled_joke(for_user_id=None)
    assert joke is not None

    await update.inline_query.answer(
        results=[
            InlineQueryResultArticle(
                id="joke",
                title="جوک بگو",
                input_message_content=InputTextMessageContent(
                    message_text=format_text_joke(joke)
                ),
                reply_markup=score_inline_keyboard_markup(joke),
            )
        ],
        is_personal=True,
        cache_time=5,
    )
