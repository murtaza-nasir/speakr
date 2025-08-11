#!/bin/bash
# Manual migration script for Docker deployments
# NOTE: The first 10 recordings are processed automatically on startup.
# This script is for processing any remaining recordings.

echo "🎯 Inquire Mode Manual Migration for Docker"
echo "============================================="

# Check if container is running
if ! docker-compose ps | grep -q "speakr.*Up"; then
    echo "❌ Speakr container is not running. Please start it first with:"
    echo "   docker-compose up -d"
    exit 1
fi

echo "ℹ️  Note: The first 10 recordings are processed automatically on startup."
echo "ℹ️  This script processes any remaining recordings that need chunking."
echo ""
echo "🔍 Checking how many recordings still need processing..."

# First, do a dry run to see what would be processed
docker-compose exec app python migrate_existing_recordings.py --dry-run

echo ""
echo "⚠️  Do you want to proceed with processing these recordings?"
echo "⚠️  This will create embeddings and may take several minutes."
read -p "Continue? (y/N): " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "🚀 Starting migration..."
    docker-compose exec app python migrate_existing_recordings.py --process --batch-size 5
    
    if [ $? -eq 0 ]; then
        echo "✅ Migration completed successfully!"
        echo "🎉 Your existing recordings are now ready for Inquire Mode!"
    else
        echo "❌ Migration failed. Check the logs above for details."
        exit 1
    fi
else
    echo "❌ Migration cancelled."
    exit 0
fi