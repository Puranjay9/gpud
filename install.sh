#!/usr/bin/env bash
set -e

echo "Installing GPUD..."

# Requirements check
if ! command -v git &> /dev/null; then
    echo "❌ Error: git is not installed."
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 is not installed."
    exit 1
fi

if ! command -v pip3 &> /dev/null; then
    echo "❌ Error: pip3 is not installed."
    exit 1
fi

# Clone directory
INSTALL_DIR="$HOME/.local/share/gpud"

if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "Cloning repository..."
    git clone https://github.com/Puranjay9/gpud.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "Installing Python dependencies..."
# Create a virtual environment to avoid PEP 668 externally-managed-environment errors
python3 -m venv .venv
./.venv/bin/pip install -e .

# Ensure the executable is available in the local bin directory
mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_DIR/.venv/bin/gpud" "$HOME/.local/bin/gpud"

# Try to automatically add to PATH if not present
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo "  Adding $HOME/.local/bin to your PATH..."
    
    if [ -f "$HOME/.bashrc" ]; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    fi
    if [ -f "$HOME/.zshrc" ]; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
    fi
    
    echo " Successfully added to PATH. Please restart your terminal or run:"
    echo "   source ~/.bashrc  (or source ~/.zshrc)"
fi

echo " Installation complete!"
echo " Run 'gpud --help' to get started."
