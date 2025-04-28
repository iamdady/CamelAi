from collections import defaultdict
from typing import Literal, Optional, Union

import discord
from discord import Message as DiscordMessage, app_commands
import logging
from src.base import Message, Conversation, ThreadConfig
from src.constants import (
    BOT_INVITE_URL,
    DISCORD_BOT_TOKEN,
    EXAMPLE_CONVOS,
    ACTIVATE_THREAD_PREFX,
    MAX_THREAD_MESSAGES,
    SECONDS_DELAY_RECEIVING_MSG,
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
)
import asyncio
from src.utils import (
    logger,
    should_block,
    close_thread,
    is_last_message_stale,
    discord_message_to_message,
)
from src import completion
from src.completion import generate_completion_response, process_response
from src.moderation import (
    moderate_message,
    send_moderation_blocked_message,
    send_moderation_flagged_message,
)

logging.basicConfig(
    format="[%(asctime)s] [%(filename)s:%(lineno)d] %(message)s", level=logging.INFO
)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
thread_data = defaultdict()


@client.event
async def on_ready():
    logger.info(f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
    completion.MY_BOT_NAME = client.user.name
    completion.MY_BOT_EXAMPLE_CONVOS = []
    for c in EXAMPLE_CONVOS:
        messages = []
        for m in c.messages:
            if m.user == "Lenard":
                messages.append(Message(user=client.user.name, text=m.text))
            else:
                messages.append(m)
        completion.MY_BOT_EXAMPLE_CONVOS.append(Conversation(messages=messages))
    await tree.sync()


# /chat message:
@tree.command(name="chat", description="Create a new thread for conversation")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
@app_commands.describe(message="The first prompt to start the chat with")
@app_commands.describe(model="The model to use for the chat")
@app_commands.describe(
    temperature="Controls randomness. Higher values mean more randomness. Between 0 and 1"
)
@app_commands.describe(
    max_tokens="How many tokens the model should output at max for each message."
)
async def chat_command(
    int: discord.Interaction,
    message: str,
    model: AVAILABLE_MODELS = DEFAULT_MODEL,
    temperature: Optional[float] = 1.0,
    max_tokens: Optional[int] = 512,
):
    try:
        # Only allow in text channels
        if not isinstance(int.channel, discord.TextChannel):
            return

        # Block servers not allowed
        if should_block(guild=int.guild):
            return

        # Only allow usage in specific channels
        ALLOWED_CHANNEL_IDS = [1276206584988434617]  # <- replace with your #gen-chat channel ID
        if int.channel_id not in ALLOWED_CHANNEL_IDS:
            embed = discord.Embed(
                title="🚫 Wrong Channel",
                description="Please use this command in <#123456789012345678> only!",
                color=discord.Color.red()
            )
            await int.response.send_message(embed=embed, ephemeral=True)
            return

        user = int.user
        logger.info(f"Chat command by {user} {message[:20]}")

        # Check valid temperature
        if temperature is not None and (temperature < 0 or temperature > 1):
            await int.response.send_message(
                f"You supplied an invalid temperature: {temperature}. Must be between 0 and 1.",
                ephemeral=True,
            )
            return

        # Check valid max_tokens
        if max_tokens is not None and (max_tokens < 1 or max_tokens > 4096):
            await int.response.send_message(
                f"You supplied an invalid max_tokens: {max_tokens}. Must be between 1 and 4096.",
                ephemeral=True,
            )
            return

        try:
            # Moderate first
            flagged_str, blocked_str = moderate_message(message=message, user=user)
            await send_moderation_blocked_message(
                guild=int.guild,
                user=user,
                blocked_str=blocked_str,
                message=message,
            )
            if len(blocked_str) > 0:
                await int.response.send_message(
                    f"Your prompt has been blocked by moderation.\n{message}",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                description=f"<@{user.id}> wants to chat! 🤖💬",
                color=discord.Color.green(),
            )
            embed.add_field(name="model", value=model)
            embed.add_field(name="temperature", value=temperature, inline=True)
            embed.add_field(name="max_tokens", value=max_tokens, inline=True)
            embed.add_field(name=user.name, value=message)

            if len(flagged_str) > 0:
                embed.color = discord.Color.yellow()
                embed.title = "⚠️ This prompt was flagged by moderation."

            await int.response.send_message(embed=embed)
            response = await int.original_response()

            await send_moderation_flagged_message(
                guild=int.guild,
                user=user,
                flagged_str=flagged_str,
                message=message,
                url=response.jump_url,
            )

        except Exception as e:
            logger.exception(e)
            await int.response.send_message(
                f"Failed to start chat {str(e)}", ephemeral=True
            )
            return

        # Create the thread
        thread = await response.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} {user.name[:20]} - {message[:30]}",
            slowmode_delay=1,
            reason="gpt-bot",
            auto_archive_duration=60,
        )
        thread_data[thread.id] = ThreadConfig(
            model=model, max_tokens=max_tokens, temperature=temperature
        )

        async with thread.typing():
            # First completion
            messages = [Message(user=user.name, text=message)]
            response_data = await generate_completion_response(
                messages=messages, user=user, thread_config=thread_data[thread.id]
            )
            await process_response(
                user=user, thread=thread, response_data=response_data
            )
    except Exception as e:
        logger.exception(e)
        await int.response.send_message(
            f"Failed to start chat {str(e)}", ephemeral=True
        )


# calls for each message
@client.event
async def on_message(message: DiscordMessage):
    try:
        # Ignore stuff we don't want
        if should_block(guild=message.guild):
            return
        if message.author == client.user:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        thread = message.channel
        if thread.owner_id != client.user.id:
            return
        if thread.archived or thread.locked or not thread.name.startswith(ACTIVATE_THREAD_PREFX):
            return
        if thread.message_count > MAX_THREAD_MESSAGES:
            await close_thread(thread)
            return

        # MODERATION check first
        flagged_str, blocked_str = moderate_message(
            message=message.content, user=message.author
        )
        await send_moderation_blocked_message(
            guild=message.guild,
            user=message.author,
            blocked_str=blocked_str,
            message=message.content,
        )
        if len(blocked_str) > 0:
            try:
                await message.delete()
                await thread.send(
                    embed=discord.Embed(
                        description=f"❌ **{message.author}'s message has been deleted by moderation.**",
                        color=discord.Color.red(),
                    )
                )
                return
            except Exception:
                await thread.send(
                    embed=discord.Embed(
                        description=f"❌ **{message.author}'s message has been blocked by moderation but could not be deleted.**",
                        color=discord.Color.red(),
                    )
                )
                return
        
        # Wait to catch multiple user messages
        await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)

        # Check if this message is still the last one
        if is_last_message_stale(
            interaction_message=message,
            last_message=thread.last_message,
            bot_id=client.user.id,
        ):
            return  # Ignore, not last message anymore
        
        logger.info(
            f"Thread message to process - {message.author}: {message.content[:50]} - {thread.name} {thread.jump_url}"
        )

        # Actually generate response
        channel_messages = [
            discord_message_to_message(m)
            async for m in thread.history(limit=MAX_THREAD_MESSAGES)
        ]
        channel_messages = [x for x in channel_messages if x is not None]
        channel_messages.reverse()

        async with thread.typing():
            response_data = await generate_completion_response(
                messages=channel_messages,
                user=message.author,
                thread_config=thread_data[thread.id],
            )

        await process_response(
            user=message.author,
            thread=thread,
            response_data=response_data
        )
        
    except Exception as e:
        logger.exception(e)

client.run(DISCORD_BOT_TOKEN)
