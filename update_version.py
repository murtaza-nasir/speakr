#!/usr/bin/env python3
"""
Simple script to update the VERSION file.
Usage: python update_version.py v0.4.3
"""
import sys
import re

def update_version(new_version):
    # Validate version format (basic check)
    if not re.match(r'^v?\d+\.\d+\.\d+', new_version):
        print(f"Warning: Version '{new_version}' doesn't follow standard format (v1.2.3)")
    
    # Ensure version starts with 'v'
    if not new_version.startswith('v'):
        new_version = 'v' + new_version
    
    # Write to VERSION file
    try:
        with open('VERSION', 'w') as f:
            f.write(new_version)
        print(f"✅ Updated VERSION file to: {new_version}")
        
        # Optional: Create git tag if in a git repo
        import subprocess
        try:
            # Check if we're in a git repo
            subprocess.check_output(['git', 'status'], stderr=subprocess.DEVNULL)
            
            # Create and push tag
            subprocess.check_output(['git', 'tag', new_version], stderr=subprocess.DEVNULL)
            print(f"✅ Created git tag: {new_version}")
            
            # Ask user if they want to push
            response = input("Push tag to remote? (y/N): ").strip().lower()
            if response == 'y':
                subprocess.check_output(['git', 'push', 'origin', new_version])
                print(f"✅ Pushed tag {new_version} to remote")
            
        except subprocess.CalledProcessError:
            print("ℹ️  Not in a git repo or git tag already exists")
        except Exception as e:
            print(f"ℹ️  Git operations failed: {e}")
            
    except Exception as e:
        print(f"❌ Failed to update VERSION file: {e}")
        return False
        
    return True

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python update_version.py <version>")
        print("Example: python update_version.py v0.4.3")
        print("Example: python update_version.py 0.4.3-alpha")
        sys.exit(1)
    
    new_version = sys.argv[1]
    success = update_version(new_version)
    sys.exit(0 if success else 1)