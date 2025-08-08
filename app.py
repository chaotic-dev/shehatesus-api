from flask import Flask, request, jsonify, render_template
from datetime import datetime, timezone, timedelta
import googleapiclient.discovery
import googleapiclient.errors
import os
import logging
from cachetools import cached, TTLCache
from werkzeug.middleware.proxy_fix import ProxyFix

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

scopes = ["https://www.googleapis.com/auth/youtube.readonly"]
api_service_name = "youtube"
api_version = "v3"

app = Flask(__name__)
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
)

youtube = googleapiclient.discovery.build(
    api_service_name, api_version, developerKey=os.environ.get("GOOGLE_API_KEY")
)


@cached(cache=TTLCache(maxsize=512, ttl=timedelta(hours=12), timer=datetime.now))
def get_channel_id_from_handle(handle):
    """Get the channel ID from a YouTube channel handle."""

    # Check if the handle starts with '@'
    if not handle.startswith("@"):
        logger.error("Invalid handle format. It should start with '@'.")
        return None, "Invalid handle format. It should start with '@'."

    logger.debug(f"Fetching channel ID for handle: {handle}")

    # Prepare the request to get channel ID by handle

    args = {"part": "id", "forHandle": handle}
    try:
        request = youtube.channels().list(**args)
        response = request.execute()
    except googleapiclient.errors.HttpError as e:
        logger.error(f"HTTP error occurred: {e}")
        return None, f"HTTP error occurred: {e}"

    if not response.get("items"):
        logger.warning("Channel not found or no items returned")
        return None, "Channel not found or no items returned"

    channel_id = response["items"][0]["id"]
    logger.debug(f"Channel ID for handle {handle} is {channel_id}")

    # Return the channel ID
    return response["items"][0]["id"], None


@cached(cache=TTLCache(maxsize=512, ttl=timedelta(hours=12), timer=datetime.now))
def get_channel_id_from_username(username):
    """Get the channel ID from a YouTube channel username."""

    logger.debug(f"Fetching channel ID for username: {username}")

    # Prepare the request to get channel ID by username
    args = {"part": "id", "forUsername": username}
    try:
        request = youtube.channels().list(**args)
        response = request.execute()
    except googleapiclient.errors.HttpError as e:
        logger.error(f"HTTP error occurred: {e}")
        return None, f"HTTP error occurred: {e}"

    if not response.get("items"):
        logger.warning("Channel not found or no items returned")
        return None, "Channel not found or no items returned"

    channel_id = response["items"][0]["id"]
    logger.debug(f"Channel ID for username {username} is {channel_id}")

    # Return the channel ID
    return channel_id, None


def get_channel_id(channel):
    """Get the channel ID from a channel name or handle."""

    if channel.startswith("UC"):
        logger.debug(f"Channel ID provided: {channel}")
        return channel
    elif channel.startswith("@"):
        logger.debug(f"Channel handle provided: {channel}")
        return get_channel_id_from_handle(channel)
    else:
        logger.debug(f"Channel username provided: {channel}")
        return get_channel_id_from_username(channel)


@cached(cache=TTLCache(maxsize=512, ttl=timedelta(hours=12), timer=datetime.now))
def get_channel_info(channel_id):
    """Get channel information from YouTube API using channel ID."""

    logger.debug(f"Fetching channel info for ID: {channel_id}")

    args = {"part": "snippet", "id": channel_id}
    try:
        request = youtube.channels().list(**args)
        response = request.execute()
    except googleapiclient.errors.HttpError as e:
        logger.error(f"HTTP error occurred: {e}")
        return None, f"HTTP error occurred: {e}"

    if not response.get("items"):
        logger.warning("Channel not found or no items returned")
        return None, "Channel not found or no items returned"

    info = {
        "channel_name": response["items"][0]["snippet"].get("title", channel_id),
        "profile_pic": response["items"][0]["snippet"]
        .get("thumbnails", {})
        .get("medium", {}),
    }

    logger.debug(f"Got channel info for : {info['channel_name']}")

    return info, None


@cached(cache=TTLCache(maxsize=512, ttl=timedelta(minutes=10), timer=datetime.now))
def get_upcoming_live_videos(channel_id):
    """Get upcoming live videos for a channel using channel ID."""

    logger.debug(f"Fetching upcoming live video for channel ID: {channel_id}")

    args = {
        "part": "snippet",
        "channelId": channel_id,
        "eventType": "upcoming",
        "type": "video",
    }
    try:
        request = youtube.search().list(**args)
        response = request.execute()
    except googleapiclient.errors.HttpError as e:
        logger.error(f"HTTP error occurred: {e}")
        return None, f"HTTP error occurred: {e}"

    if not response.get("items"):
        logger.debug("No upcoming live video found")
        return None, None

    video_ids = [item["id"]["videoId"] for item in response["items"]]
    if not video_ids:
        logger.debug("No video IDs found for live streaming details")
        return None, "No video IDs found for live streaming details"

    logger.debug(f"Found {len(video_ids)} upcoming live video(s)")
    return video_ids, None


@cached(cache=TTLCache(maxsize=512, ttl=timedelta(minutes=1), timer=datetime.now))
def get_late_status(channel_id):
    """Check if the channel is late for its live stream."""

    logger.debug(f"Checking late status for channel ID: {channel_id}")

    video_ids, error = get_upcoming_live_videos(channel_id)
    if error:
        return None, error

    if not video_ids:
        logger.debug("No upcoming live videos found")
        return "NO_SCHEDULE", None

    request = youtube.videos().list(part="liveStreamingDetails", id=",".join(video_ids))
    try:
        response = request.execute()
    except googleapiclient.errors.HttpError as e:
        logger.error(f"HTTP error occurred: {e}")
        return None, f"HTTP error occurred: {e}"

    details = response.get("items", [])
    logger.debug("Checking live streaming details for %d video(s)", len(details))

    live_status = "UNKNOWN"
    # Extract relevant details from live details
    for item in details:
        details = item.get("liveStreamingDetails", {})
        if details.get("actualEndTime"):
            logger.debug("Stream has ended for video ID: %s", item["id"])
            continue  # Skip ended streams
        elif details.get("actualStartTime"):
            logger.debug("Stream is live for video ID: %s", item["id"])
            return "LIVE", None
        elif details.get("scheduledStartTime"):
            start_time = datetime.strptime(details["scheduledStartTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            logger.debug("Stream is scheduled for video ID: %s at %s", item["id"], str(start_time))
            if start_time > datetime.now(timezone.utc) + timedelta(days=7):
                logger.debug("Stream is not within the next week, skipping")
                continue # Not within a week, skip
            if start_time < datetime.now(timezone.utc):
                return "LATE", None
            else:
                live_status = "UPCOMING"
        else:
            live_status = "UNKNOWN"

    return live_status, None

@app.route("/", methods=["GET"])
def default():
    return jsonify({"routes": ["late"]})

@app.route("/late", methods=["GET"])
def check_if_late():
    channel = request.args.get("channel")
    if not channel:
        return jsonify({"error": "Missing YouTube channel argument"}), 400

    channel_id, error = get_channel_id(channel)
    if error:
        return jsonify({"query": channel, "error": error}), 500

    channel_info, error = get_channel_info(channel_id)
    if error:
        return (
            jsonify({"query": channel, "channel_id": channel_id, "error": error}),
            500,
        )

    status, error = get_late_status(channel_id)
    if error:
        return (
            jsonify({"query": channel, "channel_id": channel_id, "error": error}),
            500,
        )

    return jsonify(
        {
            "channel_id": channel_id,
            "channel_name": channel_info["channel_name"],
            "profile_pic": channel_info["profile_pic"],
            "live_status": status,
        }
    )


if __name__ == "__main__":
    """Run the Flask app"""
    app.run(debug=True)
