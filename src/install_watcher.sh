#!/bin/bash
#
# Install/uninstall на launchd watcher-а за DZI Generator.
#
# Употреба:
#   ./install_watcher.sh install    — инсталира и стартира
#   ./install_watcher.sh uninstall  — спира и премахва
#   ./install_watcher.sh status     — показва статус
#   ./install_watcher.sh logs       — показва последните 30 реда от лога
#   ./install_watcher.sh restart    — restart на watcher-а

set -e

LABEL="com.gabriel.dzi-generator.watcher"
PLIST_SRC="$(dirname "$0")/dzi-generator-watcher.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
WATCHER_SCRIPT="$HOME/dzi-generator/src/watch_vault.py"
LOG_FILE="$HOME/dzi-generator/data/watcher.log"

cmd="${1:-help}"

case "$cmd" in
  install)
    echo "📦 Инсталирам watcher..."
    
    # Sanity checks
    if [ ! -f "$WATCHER_SCRIPT" ]; then
      echo "❌ $WATCHER_SCRIPT не съществува."
      echo "   Копирай го първо в src/."
      exit 1
    fi
    
    if ! python3 -c "import watchdog" 2>/dev/null; then
      echo "❌ Липсва пакет 'watchdog'. Инсталирай:"
      echo "   pip3 install watchdog"
      exit 1
    fi
    
    if [ ! -f "$PLIST_SRC" ]; then
      echo "❌ Не намирам plist template: $PLIST_SRC"
      exit 1
    fi
    
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$HOME/dzi-generator/data"
    
    # Замени __HOME__ placeholder с реалния $HOME
    sed "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DEST"
    
    # Unload first if already loaded (non-fatal)
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    
    launchctl load "$PLIST_DEST"
    
    echo ""
    echo "✅ Инсталиран в $PLIST_DEST"
    echo ""
    echo "Watcher-ът сега работи в background. За проверка:"
    echo "   $0 status"
    echo "   $0 logs"
    echo ""
    echo "Тест: редактирай файл в Obsidian, изчакай 3-5 сек, виж лога:"
    echo "   tail -f $LOG_FILE"
    ;;
  
  uninstall)
    echo "🗑️  Премахвам watcher..."
    if [ -f "$PLIST_DEST" ]; then
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
      rm "$PLIST_DEST"
      echo "✅ Премахнат: $PLIST_DEST"
    else
      echo "⏭️  Не е инсталиран."
    fi
    ;;
  
  status)
    if launchctl list | grep -q "$LABEL"; then
      echo "✅ Watcher-ът работи:"
      launchctl list | grep "$LABEL"
    else
      echo "❌ Watcher-ът НЕ работи."
      if [ -f "$PLIST_DEST" ]; then
        echo "   plist файлът съществува, но не е зареден. Опитай: launchctl load $PLIST_DEST"
      else
        echo "   plist файлът липсва. Пусни: $0 install"
      fi
    fi
    ;;
  
  logs)
    if [ -f "$LOG_FILE" ]; then
      echo "📋 Последните 30 реда от $LOG_FILE:"
      echo ""
      tail -30 "$LOG_FILE"
    else
      echo "⏭️  Няма още лог файл: $LOG_FILE"
    fi
    ;;
  
  restart)
    echo "🔄 Restart на watcher..."
    if [ -f "$PLIST_DEST" ]; then
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
      sleep 1
      launchctl load "$PLIST_DEST"
      echo "✅ Restart-нат."
    else
      echo "❌ Не е инсталиран. Пусни: $0 install"
      exit 1
    fi
    ;;
  
  help|--help|-h|*)
    cat <<EOF
DZI Generator vault watcher manager.

Употреба:
  $0 install     Инсталира launchd plist и стартира watcher-а
  $0 uninstall   Спира и премахва watcher-а
  $0 status      Показва текущия статус
  $0 logs        Последните 30 реда от лога
  $0 restart     Restart на watcher-а (полезно след update на скрипта)
  $0 help        Тази помощ

Файлове:
  Плист:  $PLIST_DEST
  Лог:    $LOG_FILE
EOF
    ;;
esac
