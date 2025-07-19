#!/usr/bin/env python3
"""
Logo Resizer Script for Speakr
Resizes a source PNG image to all required icon sizes for PWA and favicon support.

Usage:
    python resize_logo.py <source_image.png>

Requirements:
    pip install Pillow

This script will create all the necessary icon sizes in the static/img/ directory.
"""

import sys
import os
from PIL import Image, ImageDraw
import argparse

def create_maskable_version(image, size):
    """Create a maskable version with safe zone padding (20% on all sides)"""
    # Calculate the size of the logo with padding
    logo_size = int(size * 0.6)  # Logo takes 60% of the canvas (20% padding on each side)
    
    # Create new image with transparent background
    maskable = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    
    # Resize the original logo
    logo_resized = image.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
    
    # Calculate position to center the logo
    x = (size - logo_size) // 2
    y = (size - logo_size) // 2
    
    # Paste the logo onto the center of the canvas
    maskable.paste(logo_resized, (x, y), logo_resized if logo_resized.mode == 'RGBA' else None)
    
    return maskable

def resize_logo(source_path, output_dir="static/img"):
    """Resize the source image to all required sizes"""
    
    # Check if source file exists
    if not os.path.exists(source_path):
        print(f"Error: Source file '{source_path}' not found!")
        return False
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Open the source image
        with Image.open(source_path) as img:
            # Convert to RGBA if not already (for transparency support)
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            
            print(f"Source image: {img.size[0]}x{img.size[1]} pixels")
            print(f"Output directory: {output_dir}")
            print()
            
            # Define all the sizes we need
            sizes = {
                # Essential PWA icons
                'icon-192x192.png': 192,
                'icon-512x512.png': 512,
                
                # Additional recommended icons
                'icon-16x16.png': 16,
                'icon-32x32.png': 32,
                'icon-180x180.png': 180,  # Apple touch icon
                
                # Maskable version
                'icon-maskable-512x512.png': 512,
            }
            
            # Resize to each required size
            for filename, size in sizes.items():
                output_path = os.path.join(output_dir, filename)
                
                if 'maskable' in filename:
                    # Create maskable version with safe zone
                    resized = create_maskable_version(img, size)
                    print(f"✓ Created maskable icon: {filename} ({size}x{size})")
                else:
                    # Regular resize
                    resized = img.resize((size, size), Image.Resampling.LANCZOS)
                    print(f"✓ Created icon: {filename} ({size}x{size})")
                
                # Save the resized image
                resized.save(output_path, 'PNG', optimize=True)
            
            print()
            print("🎉 All icons created successfully!")
            print()
            print("Next steps:")
            print("1. Replace static/img/favicon.svg with your SVG version (if you have one)")
            print("2. Clear browser cache and test the new icons")
            print("3. Test PWA installation to verify icons appear correctly")
            
            return True
            
    except Exception as e:
        print(f"Error processing image: {e}")
        return False

def create_ico_favicon(source_path, output_dir="static/img"):
    """Create a multi-size ICO favicon file"""
    try:
        with Image.open(source_path) as img:
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            
            # Create different sizes for the ICO file
            sizes = [16, 32, 48]
            images = []
            
            for size in sizes:
                resized = img.resize((size, size), Image.Resampling.LANCZOS)
                images.append(resized)
            
            # Save as ICO file
            ico_path = os.path.join(output_dir, 'favicon.ico')
            images[0].save(ico_path, format='ICO', sizes=[(img.width, img.height) for img in images])
            print(f"✓ Created favicon.ico with sizes: {sizes}")
            
    except Exception as e:
        print(f"Warning: Could not create favicon.ico: {e}")

def main():
    parser = argparse.ArgumentParser(description='Resize logo for Speakr PWA icons')
    parser.add_argument('source', help='Source PNG image file')
    parser.add_argument('--output-dir', default='static/img', help='Output directory (default: static/img)')
    parser.add_argument('--create-ico', action='store_true', help='Also create favicon.ico file')
    
    args = parser.parse_args()
    
    print("🎨 Speakr Logo Resizer")
    print("=" * 50)
    
    # Resize the logo
    success = resize_logo(args.source, args.output_dir)
    
    if success and args.create_ico:
        print()
        create_ico_favicon(args.source, args.output_dir)
    
    if success:
        print()
        print("📁 Files created in", args.output_dir + ":")
        for file in os.listdir(args.output_dir):
            if file.startswith('icon-') and file.endswith('.png'):
                file_path = os.path.join(args.output_dir, file)
                size = os.path.getsize(file_path)
                print(f"   {file} ({size:,} bytes)")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resize_logo.py <source_image.png>")
        print("Example: python resize_logo.py my_logo.png")
        sys.exit(1)
    
    main()
