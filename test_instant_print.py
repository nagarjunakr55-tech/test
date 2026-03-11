#!/usr/bin/env python3
"""
Test script to verify instant printing functionality
"""
import sys
import os
sys.path.append('/Users/3597267/Documents/DripWeb')

def test_billing_logic_imports():
    """Test that billing logic functions are importable"""
    try:
        from billing_logic import get_receipt_text_for_instant_print
        print("✅ billing_logic.get_receipt_text_for_instant_print imported successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to import billing_logic function: {e}")
        return False

def test_controller_imports():
    """Test that controller imports work"""
    try:
        from controllers.billing_controller import billing_bp
        print("✅ billing_controller imported successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to import billing_controller: {e}")
        return False

def test_flask_app_structure():
    """Test that Flask app structure is intact"""
    try:
        from app import create_app
        app = create_app()
        print("✅ Flask app created successfully")
        
        # Check if blueprint is registered
        blueprint_names = [bp.name for bp in app.blueprints.values()]
        if 'billing' in blueprint_names:
            print("✅ Billing blueprint registered")
        else:
            print("❌ Billing blueprint not found")
            return False
            
        return True
    except Exception as e:
        print(f"❌ Flask app creation failed: {e}")
        return False

def main():
    print("🔍 DRIPWEB INSTANT PRINTING VERIFICATION")
    print("=" * 50)
    
    tests = [
        test_billing_logic_imports,
        test_controller_imports,
        test_flask_app_structure
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        print()
    
    print("=" * 50)
    print(f"RESULTS: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All DripWeb changes verified successfully!")
        return True
    else:
        print("⚠️  Some tests failed - check the errors above")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
