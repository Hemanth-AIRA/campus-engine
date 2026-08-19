#!/bin/bash
#
# AIRA WebRTC Audio Configuration Script
# =====================================
# 
# This script allows you to quickly enable/disable WebRTC audio integration
# Based on your log showing WebRTC audio timeouts, you can disable it to use
# the reliable system audio fallback exclusively.

echo "🎵 AIRA WebRTC Audio Configuration"
echo "=================================="
echo ""

# Check current setting
current_setting=$(echo $WEBRTC_AUDIO_ENABLED)
if [ -z "$current_setting" ]; then
    current_setting="true (default)"
fi

echo "📊 Current WebRTC Audio Setting: $current_setting"
echo ""

echo "Choose an option:"
echo "1) Enable WebRTC Audio Integration (default)"
echo "2) Disable WebRTC Audio (use reliable system audio only)"
echo "3) Show current configuration"
echo ""

read -p "Enter your choice (1-3): " choice

case $choice in
    1)
        echo ""
        echo "✅ Enabling WebRTC Audio Integration..."
        export WEBRTC_AUDIO_ENABLED=true
        echo "export WEBRTC_AUDIO_ENABLED=true" >> ~/.bashrc
        echo "🎵 WebRTC Audio is now ENABLED"
        echo "ℹ️  The system will attempt WebRTC audio first, then fallback to system audio"
        ;;
    2)
        echo ""
        echo "🔧 Disabling WebRTC Audio Integration..."
        export WEBRTC_AUDIO_ENABLED=false
        echo "export WEBRTC_AUDIO_ENABLED=false" >> ~/.bashrc
        echo "🎵 WebRTC Audio is now DISABLED"
        echo "✅ The system will use reliable system audio exclusively"
        echo "⚡ This eliminates all WebRTC audio timeout issues"
        ;;
    3)
        echo ""
        echo "📋 Current Configuration:"
        echo "WEBRTC_AUDIO_ENABLED = $WEBRTC_AUDIO_ENABLED"
        echo ""
        if [ "$WEBRTC_AUDIO_ENABLED" = "false" ]; then
            echo "🔧 WebRTC audio is DISABLED - using system audio only"
            echo "✅ No WebRTC audio timeouts will occur"
        else
            echo "🎵 WebRTC audio is ENABLED - attempting WebRTC first with fallback"
            echo "ℹ️  If timeouts occur, consider disabling with option 2"
        fi
        ;;
    *)
        echo ""
        echo "❌ Invalid choice. Please run the script again and choose 1, 2, or 3."
        exit 1
        ;;
esac

echo ""
echo "🔄 Please restart your terminal or run 'source ~/.bashrc' for changes to take effect"
echo "🚀 Your WebRTC interview system configuration has been updated"
