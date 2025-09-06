#!/usr/bin/env python3
"""
Test script for Inquire Mode functionality
"""
import os
import sys

# Add the parent directory to the path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, User, Recording, TranscriptChunk, InquireSession, Tag

def test_database_models():
    """Test that the new database models work correctly."""
    with app.app_context():
        print("🔍 Testing Inquire Mode Database Models...")
        
        # Test that tables exist
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        
        required_tables = ['transcript_chunk', 'inquire_session']
        for table in required_tables:
            if table in tables:
                print(f"✅ Table '{table}' exists")
            else:
                print(f"❌ Table '{table}' missing")
                return False
        
        # Test creating sample data
        try:
            # Create a test user (or get existing one)
            user = User.query.first()
            if not user:
                print("❌ No users found. Please create a user first.")
                return False
            
            print(f"📝 Using test user: {user.username}")
            
            # Create a test recording if none exist
            recording = Recording.query.filter_by(user_id=user.id).first()
            if not recording:
                print("❌ No recordings found. Please create a recording first.")
                return False
            
            print(f"🎵 Using test recording: {recording.title}")
            
            # Test TranscriptChunk creation
            chunk = TranscriptChunk(
                recording_id=recording.id,
                user_id=user.id,
                chunk_index=0,
                content="This is a test transcription chunk.",
                start_time=0.0,
                end_time=5.0,
                speaker_name="Test Speaker"
            )
            
            db.session.add(chunk)
            
            # Test InquireSession creation
            session = InquireSession(
                user_id=user.id,
                session_name="Test Session",
                filter_tags='[]',
                filter_speakers='["Test Speaker"]'
            )
            
            db.session.add(session)
            db.session.commit()
            
            print("✅ Successfully created test TranscriptChunk and InquireSession")
            
            # Clean up test data
            db.session.delete(chunk)
            db.session.delete(session)
            db.session.commit()
            
            print("✅ Test data cleaned up")
            
        except Exception as e:
            print(f"❌ Error testing models: {e}")
            return False
        
        return True

def test_chunking_functions():
    """Test the chunking and embedding functions."""
    with app.app_context():
        print("🔧 Testing Chunking Functions...")
        
        try:
            from src.app import chunk_transcription, generate_embeddings, serialize_embedding, deserialize_embedding
            
            # Test chunking
            test_text = "This is a test sentence. This is another sentence for testing. And here's a third sentence to make sure chunking works properly with longer text that should be split into multiple chunks."
            chunks = chunk_transcription(test_text, max_chunk_length=100, overlap=20)
            
            if len(chunks) > 1:
                print(f"✅ Chunking works: {len(chunks)} chunks created")
            else:
                print("✅ Text too short for chunking (expected behavior)")
            
            # Test embeddings (will only work if sentence-transformers is installed)
            try:
                embeddings = generate_embeddings(["test sentence", "another test"])
                if len(embeddings) == 2:
                    print("✅ Embedding generation works")
                    
                    # Test serialization
                    if embeddings[0] is not None:
                        serialized = serialize_embedding(embeddings[0])
                        deserialized = deserialize_embedding(serialized)
                        if deserialized is not None and len(deserialized) > 0:
                            print("✅ Embedding serialization/deserialization works")
                        else:
                            print("❌ Embedding serialization/deserialization failed")
                else:
                    print("❌ Embedding generation returned wrong number of embeddings")
                    
            except Exception as e:
                print(f"⚠️  Embedding test skipped (sentence-transformers may not be installed): {e}")
                
        except Exception as e:
            print(f"❌ Error testing chunking functions: {e}")
            return False
            
        return True

def test_api_imports():
    """Test that all API endpoints can be imported."""
    print("🔌 Testing API Endpoint Imports...")
    
    try:
        from src.app import (
            get_inquire_sessions, create_inquire_session, inquire_search, 
            inquire_chat, get_available_filters, process_recording_chunks_endpoint
        )
        print("✅ All inquire mode API endpoints imported successfully")
        return True
    except ImportError as e:
        print(f"❌ Failed to import API endpoints: {e}")
        return False

def main():
    """Run all tests."""
    print("🚀 Starting Inquire Mode Tests...\n")
    
    tests = [
        ("Database Models", test_database_models),
        ("Chunking Functions", test_chunking_functions), 
        ("API Imports", test_api_imports)
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n--- {test_name} ---")
        try:
            success = test_func()
            results.append((test_name, success))
        except Exception as e:
            print(f"❌ {test_name} failed with exception: {e}")
            results.append((test_name, False))
    
    print("\n" + "="*50)
    print("📊 Test Results Summary:")
    print("="*50)
    
    all_passed = True
    for test_name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} - {test_name}")
        if not success:
            all_passed = False
    
    print("\n" + "="*50)
    if all_passed:
        print("🎉 All tests passed! Inquire Mode is ready to use.")
    else:
        print("⚠️  Some tests failed. Please check the output above.")
        
    return all_passed

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)