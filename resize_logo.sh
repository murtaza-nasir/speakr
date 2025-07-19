#!/bin/bash

# Logo Resizer Script for Speakr (ImageMagick version)
# Resizes a source PNG image to all required icon sizes for PWA and favicon support.
#
# Usage: ./resize_logo.sh <source_image.png>
# Requirements: ImageMagick (sudo apt install imagemagick)

set -e

# Check if source file is provided
if [ $# -eq 0 ]; then
    echo "Usage: $0 <source_image.png>"
    echo "Example: $0 my_logo.png"
    exit 1
fi

SOURCE_FILE="$1"
OUTPUT_DIR="static/img"

# Check if source file exists
if [ ! -f "$SOURCE_FILE" ]; then
    echo "Error: Source file '$SOURCE_FILE' not found!"
    exit 1
fi

# Check if ImageMagick is installed
if ! command -v convert &> /dev/null; then
    echo "Error: ImageMagick is not installed!"
    echo "Install it with: sudo apt install imagemagick"
    exit 1
fi

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

echo "🎨 Speakr Logo Resizer (ImageMagick)"
echo "=================================================="
echo "Source file: $SOURCE_FILE"
echo "Output directory: $OUTPUT_DIR"
echo

# Define sizes
declare -A SIZES=(
    ["icon-16x16.png"]=16
    ["icon-32x32.png"]=32
    ["icon-180x180.png"]=180
    ["icon-192x192.png"]=192
    ["icon-512x512.png"]=512
)

# Resize to each size
for filename in "${!SIZES[@]}"; do
    size=${SIZES[$filename]}
    output_path="$OUTPUT_DIR/$filename"
    
    convert "$SOURCE_FILE" -resize "${size}x${size}" "$output_path"
    echo "✓ Created icon: $filename (${size}x${size})"
done

# Create maskable version with padding
echo "✓ Creating maskable icon with safe zone..."
convert "$SOURCE_FILE" -resize 307x307 -gravity center -extent 512x512 -background transparent "$OUTPUT_DIR/icon-maskable-512x512.png"
echo "✓ Created maskable icon: icon-maskable-512x512.png (512x512)"

# Create favicon.ico (optional)
if command -v convert &> /dev/null; then
    echo "✓ Creating favicon.ico..."
    convert "$SOURCE_FILE" -resize 16x16 -resize 32x32 -resize 48x48 "$OUTPUT_DIR/favicon.ico"
    echo "✓ Created favicon.ico"
fi

echo
echo "🎉 All icons created successfully!"
echo
echo "📁 Files created:"
ls -la "$OUTPUT_DIR"/icon-*.png "$OUTPUT_DIR"/favicon.ico 2>/dev/null || true

echo
echo "Next steps:"
echo "1. Replace static/img/favicon.svg with your SVG version (if you have one)"
echo "2. Clear browser cache and test the new icons"
echo "3. Test PWA installation to verify icons appear correctly"
