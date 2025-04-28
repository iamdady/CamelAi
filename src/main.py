from collections import defaultdict
from typing import Literal, Optional, Union
import datetime
import asyncio
import logging

import discord
from discord import Message as DiscordMessage, app_commands
from discord.ext import tasks

from src.base import Message, Conversation, ThreadConfig
from src.constants import (
    BOT_INVITE_URL,
    DISCORD_BOT_TOKEN,
    EXAMPLE_CONVOS,
    MAX_MESSAGES,
    SECONDS_DELAY_RECEIVING_MSG,
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
)
from src.utils import (
    logger,
    should_block,
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

# Configuration constants
VERIFIED_ROLE_ID = 1276036033900712027
SERVER_OWNER_ID = 430967314016632844
AI_CHATS_CATEGORY_ID = 1366330359805116547
CHANNEL_PREFIX = "ai-chat-"
INACTIVITY_REMINDER_MINUTES = 15
INACTIVITY_CLOSE_MINUTES = 30
REMINDER_MESSAGE = "{user.mention}, this AI chat will close automatically if no activity happens in the next 15 minutes!"

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# Dictionary to store channel data: {channel_id: {"config": ThreadConfig, "last_activity": datetime}}
channel_data = {}


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
    
    # Start the background task to check for inactive channels
    check_inactive_channels.start()
    await tree.sync()


def has_verified_role():
    """Check if the user has the verified role"""
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id == SERVER_OWNER_ID:
            return True
        role = discord.utils.get(interaction.user.roles, id=VERIFIED_ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "You need the Verified role to use this command.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


@tasks.loop(minutes=1)
async def check_inactive_channels():
    """Check for inactive channels and send reminders or delete them based on inactivity time"""
    current_time = datetime.datetime.now()
    channels_to_close = []
    
    for channel_id, data in list(channel_data.items()):
        last_activity = data["last_activity"]
        inactive_minutes = (current_time - last_activity).total_seconds() / 60
        user_id = data["user_id"]
        
        # Send reminder at 15 minutes
        if inactive_minutes >= INACTIVITY_REMINDER_MINUTES and not data.get("reminder_sent", False):
            channel = client.get_channel(channel_id)
            if channel:
                user = await client.fetch_user(user_id)
                if user:
                    try:
                        await channel.send(REMINDER_MESSAGE.format(user=user))
                        channel_data[channel_id]["reminder_sent"] = True
                    except Exception as e:
                        logger.error(f"Failed to send reminder in channel {channel_id}: {str(e)}")
        
        # Close channel at 30 minutes
        if inactive_minutes >= INACTIVITY_CLOSE_MINUTES:
            channels_to_close.append(channel_id)
    
    # Close channels that need to be closed
    for channel_id in channels_to_close:
        channel = client.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason="Closed due to inactivity")
                logger.info(f"Deleted channel {channel.name} due to inactivity")
            except Exception as e:
                logger.error(f"Failed to delete channel {channel_id}: {str(e)}")
        
        # Remove channel from our tracking
        if channel_id in channel_data:
            del channel_data[channel_id]


@check_inactive_channels.before_loop
async def before_check_inactive_channels():
    await client.wait_until_ready()


# /chat command
@tree.command(name="chat", description="Create a new private channel for AI conversation")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True, view_channel=True, manage_channels=True)
@has_verified_role()
@app_commands.describe(message="The first prompt to start the chat with")
@app_commands.describe(model="The model to use for the chat")
@app_commands.describe(temperature="Controls randomness. Higher values mean more randomness. Between 0 and 1")
@app_commands.describe(max_tokens="How many tokens the model should output at max for each message.")
async def chat_command(
    interaction: discord.Interaction,
    message: str,
    model: AVAILABLE_MODELS = DEFAULT_MODEL,
    temperature: Optional[float] = 1.0,
    max_tokens: Optional[int] = 512,
):
    try:
        # Block servers not allowed
        if should_block(guild=interaction.guild):
            return

        user = interaction.user
        logger.info(f"Chat command by {user} {message[:20]}")

        # Check for valid settings
        if temperature is not None and (temperature < 0 or temperature > 1):
            await interaction.response.send_message(
                f"Invalid temperature: {temperature}. Must be between 0 and 1.",
                ephemeral=True,
            )
            return

        if max_tokens is not None and (max_tokens < 1 or max_tokens > 4096):
            await interaction.response.send_message(
                f"Invalid max_tokens: {max_tokens}. Must be between 1 and 4096.",
                ephemeral=True,
            )
            return

        # Check if user already has an active chat channel
        for channel_id, data in channel_data.items():
            if data["user_id"] == user.id:
                channel = client.get_channel(channel_id)
                if channel:
                    await interaction.response.send_message(
                        f"You already have an open AI chat: {channel.mention}",
                        ephemeral=True,
                    )
                    return

        try:
            # Moderate
            flagged_str, blocked_str = moderate_message(message=message, user=user)
            await send_moderation_blocked_message(
                guild=interaction.guild,
                user=user,
                blocked_str=blocked_str,
                message=message,
            )
            if len(blocked_str) > 0:
                await interaction.response.send_message(
                    f"Your prompt was blocked.\n{message}",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Get AI Chats category
            category = client.get_channel(AI_CHATS_CATEGORY_ID)
            if not category:
                await interaction.followup.send(
                    "AI Chats category not found. Please contact an administrator.",
                    ephemeral=True,
                )
                return

            # Create private channel
            channel_name = f"{CHANNEL_PREFIX}{user.name.lower()}"
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                client.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            # Add server owner to overwrites
            server_owner = await client.fetch_user(SERVER_OWNER_ID)
            if server_owner:
                owner_member = await interaction.guild.fetch_member(SERVER_OWNER_ID)
                if owner_member:
                    overwrites[owner_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            chat_channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"AI Chat for {user.name}"
            )

            # Store channel data
            channel_data[chat_channel.id] = {
                "config": ThreadConfig(model=model, max_tokens=max_tokens, temperature=temperature),
                "last_activity": datetime.datetime.now(),
                "user_id": user.id,
                "reminder_sent": False
            }

            # Send initial message with embed
            embed = discord.Embed(
                title="AI Chat Session Started",
                description=f"{user.mention} has started a new AI chat session!",
                color=discord.Color.blue()
            )
            embed.add_field(name="Model", value=model)
            embed.add_field(name="Temperature", value=temperature)
            embed.add_field(name="Max Tokens", value=max_tokens)
            await chat_channel.send(content=f"{user.mention}", embed=embed)

            # Send flagged message if needed
            if len(flagged_str) > 0:
                warning_embed = discord.Embed(
                    title="⚠️ Flagged by moderation",
                    description=f"Your message was flagged but allowed.",
                    color=discord.Color.yellow()
                )
                await chat_channel.send(embed=warning_embed)
                
                await send_moderation_flagged_message(
                    guild=interaction.guild,
                    user=user,
                    flagged_str=flagged_str,
                    message=message,
                    url=chat_channel.jump_url,
                )

            # Send user's initial message
            initial_message = await chat_channel.send(f"**{user.name}**: {message}")

            # Generate AI response
            async with chat_channel.typing():
                messages = [Message(user=user.name, text=message)]
                response_data = await generate_completion_response(
                    messages=messages,
                    user=user,
                    thread_config=channel_data[chat_channel.id]["config"],
                )

                await process_response(
                    user=user,
                    thread=chat_channel,  # Using channel in place of thread
                    response_data=response_data,
                )

            # Notify user in ephemeral message
            await interaction.followup.send(
                f"Chat started in {chat_channel.mention}",
                ephemeral=True,
            )

        except Exception as e:
            logger.exception(e)
            await interaction.followup.send(
                f"Failed to start chat: {str(e)}", ephemeral=True
            )
            return

    except Exception as e:
        logger.exception(e)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Failed to create chat: {str(e)}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Failed to create chat: {str(e)}", ephemeral=True
            )


@client.event
async def on_message(message: DiscordMessage):
    try:
        # Ignore messages from the bot itself
        if message.author == client.user:
            return

        # Ignore non-text messages or empty messages
        if not message.content or message.content.strip() == "":
            return

        # Ignore messages in servers that should be blocked
        if should_block(guild=message.guild):
            return

        # Check if this is a message in one of our AI chat channels
        if message.channel.id not in channel_data:
            return

        # Update the last activity time for this channel
        channel_data[message.channel.id]["last_activity"] = datetime.datetime.now()
        # Reset reminder flag when there's new activity
        channel_data[message.channel.id]["reminder_sent"] = False

        # Moderate
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
                await message.channel.send(
                    embed=discord.Embed(
                        description=f"❌ {message.author.mention}'s message deleted by moderation.",
                        color=discord.Color.red(),
                    )
                )
                return
            except Exception:
                await message.channel.send(
                    embed=discord.Embed(
                        description=f"❌ {message.author.mention}'s message blocked but couldn't delete it.",
                        color=discord.Color.red(),
                    )
                )
                return

        await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)

        # Check if another message from the bot was sent after this user's message
        async for last_msg in message.channel.history(limit=1):
            if last_msg.author == client.user and last_msg.id != message.id:
                # Bot already responded to a newer message
                return

        logger.info(
            f"Channel message to process - {message.author}: {message.content[:50]} - {message.channel.name}"
        )

        # Collect message history
        channel_messages = [
            discord_message_to_message(m)
            async for m in message.channel.history(limit=MAX_MESSAGES)
        ]
        channel_messages = [x for x in channel_messages if x is not None]
        channel_messages.reverse()

        # Generate AI response
        async with message.channel.typing():
            response_data = await generate_completion_response(
                messages=channel_messages,
                user=message.author,
                thread_config=channel_data[message.channel.id]["config"],
            )

            await process_response(
                user=message.author,
                thread=message.channel,  # Using channel in place of thread
                response_data=response_data,
            )
            
            # Update last activity time after bot responds
            channel_data[message.channel.id]["last_activity"] = datetime.datetime.now()

    except Exception as e:
        logger.exception(e)


client.run(DISCORD_BOT_TOKEN)
