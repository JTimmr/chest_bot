# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY holder_leaderboard_bot.py .
COPY snapshot_fartboy_holders.py .
COPY incoming_tracker.py .

# Run the bot
CMD ["python", "holder_leaderboard_bot.py"]
