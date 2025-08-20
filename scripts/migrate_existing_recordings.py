#!/usr/bin/env python3
"""
Migration script to process existing recordings for Inquire Mode.
This script will chunk and vectorize all existing recordings that haven't been processed yet.
"""
import os
import sys
from app import app, db, Recording, TranscriptChunk, process_recording_chunks

def count_recordings_needing_processing():
    """Count how many recordings need chunk processing."""
    with app.app_context():
        # Get all completed recordings
        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
        
        # Check which ones don't have chunks
        recordings_needing_processing = []
        for recording in completed_recordings:
            if recording.transcription:  # Has transcription
                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                if chunk_count == 0:  # No chunks yet
                    recordings_needing_processing.append(recording)
        
        return recordings_needing_processing

def migrate_existing_recordings(batch_size=10, dry_run=False):
    """
    Process existing recordings in batches to create chunks and embeddings.
    
    Args:
        batch_size (int): Number of recordings to process at once
        dry_run (bool): If True, just show what would be processed
    """
    with app.app_context():
        recordings_to_process = count_recordings_needing_processing()
        
        print(f"🔍 Found {len(recordings_to_process)} recordings that need chunk processing")
        
        if len(recordings_to_process) == 0:
            print("✅ All recordings are already processed!")
            return True
        
        if dry_run:
            print("\n📋 Recordings that would be processed:")
            for i, recording in enumerate(recordings_to_process, 1):
                print(f"  {i}. {recording.title} (ID: {recording.id}) - {len(recording.transcription)} chars")
            print(f"\nThis is a dry run. Use --process to actually run the migration.")
            return True
        
        print(f"🚀 Processing {len(recordings_to_process)} recordings in batches of {batch_size}")
        
        processed = 0
        errors = 0
        
        for i in range(0, len(recordings_to_process), batch_size):
            batch = recordings_to_process[i:i + batch_size]
            print(f"\n📦 Processing batch {i//batch_size + 1} ({len(batch)} recordings)...")
            
            for recording in batch:
                try:
                    print(f"  ⏳ Processing: {recording.title} (ID: {recording.id})")
                    
                    success = process_recording_chunks(recording.id)
                    if success:
                        processed += 1
                        # Get chunk count to report
                        chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                        print(f"    ✅ Created {chunk_count} chunks")
                    else:
                        errors += 1
                        print(f"    ❌ Failed to process recording {recording.id}")
                        
                except Exception as e:
                    errors += 1
                    print(f"    ❌ Error processing recording {recording.id}: {e}")
            
            # Commit batch
            try:
                db.session.commit()
                print(f"  💾 Batch committed successfully")
            except Exception as e:
                db.session.rollback()
                print(f"  ❌ Error committing batch: {e}")
                errors += len(batch)
        
        print(f"\n📊 Migration Summary:")
        print(f"  ✅ Successfully processed: {processed}")
        print(f"  ❌ Errors: {errors}")
        print(f"  📈 Success rate: {(processed/(processed+errors)*100):.1f}%" if (processed+errors) > 0 else "N/A")
        
        return errors == 0

def main():
    """Main function to handle command line arguments."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Migrate existing recordings for Inquire Mode')
    parser.add_argument('--dry-run', action='store_true', 
                       help='Show what would be processed without actually processing')
    parser.add_argument('--process', action='store_true',
                       help='Actually process the recordings')
    parser.add_argument('--batch-size', type=int, default=10,
                       help='Number of recordings to process in each batch (default: 10)')
    
    args = parser.parse_args()
    
    if not args.dry_run and not args.process:
        print("❌ Please specify either --dry-run or --process")
        print("Use --help for more information")
        return False
    
    print("🎯 Inquire Mode Migration Tool")
    print("=" * 40)
    
    try:
        if args.dry_run:
            success = migrate_existing_recordings(args.batch_size, dry_run=True)
        else:
            print("⚠️  This will process all existing recordings and create embeddings.")
            print("⚠️  This may take a while and use significant CPU/memory.")
            
            confirm = input("Continue? (y/N): ")
            if confirm.lower() != 'y':
                print("❌ Migration cancelled by user")
                return False
            
            success = migrate_existing_recordings(args.batch_size, dry_run=False)
        
        return success
        
    except KeyboardInterrupt:
        print("\n❌ Migration cancelled by user")
        return False
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)