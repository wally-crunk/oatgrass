#!/usr/bin/env python3
"""
Convenience shim to run Oatgrass from a source checkout.
Usage: python oatgrass.py [--verify|--help|--config PATH]
"""

from oatgrass.cli import main


if __name__ == "__main__":
    main()
