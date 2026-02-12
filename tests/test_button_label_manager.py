"""
Quick smoke test for ButtonLabelManager.

Run this to verify basic functionality works.
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import workflow_editor
sys.path.insert(0, str(Path(__file__).parent.parent))

from workflow_editor.core.button_labels import ButtonLabelManager, DEFAULT_BUTTON_LABELS
from workflow_editor.llm.backend_base import LLMTask


def test_button_label_manager():
    """Test basic ButtonLabelManager functionality."""
    print("Testing ButtonLabelManager...")
    
    # Create a temp config path (won't exist, should handle gracefully)
    config_path = Path(__file__).parent / "temp_config.json"
    manager = ButtonLabelManager(config_path)
    
    # Test 1: Get default labels
    print("\n✓ Test 1: Default labels")
    for task in DEFAULT_BUTTON_LABELS:
        label = manager.get_label(task)
        print(f"  {task.value}: '{label}'")
    
    # Test 2: Set custom label
    print("\n✓ Test 2: Set custom label")
    success = manager.set_label(LLMTask.DERIVE_JSON_FROM_TEXT, "text_json", "Custom Label")
    assert success, "Failed to set custom label"
    custom_label = manager.get_label(LLMTask.DERIVE_JSON_FROM_TEXT, tab_id="text_json")
    print(f"  Custom label: '{custom_label}'")
    assert custom_label == "Custom Label"
    
    # Test 3: Force default
    print("\n✓ Test 3: Force default")
    default_label = manager.get_label(LLMTask.DERIVE_JSON_FROM_TEXT, tab_id="text_json", force_default=True)
    print(f"  Default label (forced): '{default_label}'")
    assert default_label == DEFAULT_BUTTON_LABELS[LLMTask.DERIVE_JSON_FROM_TEXT]
    
    # Test 4: Clear label
    print("\n✓ Test 4: Clear label")
    cleared = manager.clear_label(LLMTask.DERIVE_JSON_FROM_TEXT, "text_json")
    assert cleared, "Failed to clear label"
    restored_label = manager.get_label(LLMTask.DERIVE_JSON_FROM_TEXT, tab_id="text_json")
    print(f"  Restored to default: '{restored_label}'")
    assert restored_label == DEFAULT_BUTTON_LABELS[LLMTask.DERIVE_JSON_FROM_TEXT]
    
    # Test 5: Validation
    print("\n✓ Test 5: Validation")
    invalid = manager.set_label(LLMTask.REVIEW_JSON, "json_code", "")
    assert not invalid, "Should reject empty label"
    print("  Empty label rejected correctly")
    
    # Test 6: Multiple labels per tab
    print("\n✓ Test 6: Multiple labels per tab")
    manager.set_label(LLMTask.GENERATE_CODE_FROM_JSON, "json_code", "Gen Code")
    manager.set_label(LLMTask.DERIVE_JSON_FROM_CODE, "json_code", "Parse Code")
    custom_labels = manager.get_all_custom_labels("json_code")
    print(f"  json_code has {len(custom_labels)} custom labels")
    assert len(custom_labels) == 2
    
    # Test 7: Clear all labels for tab
    print("\n✓ Test 7: Clear tab labels")
    count = manager.clear_tab_labels("json_code")
    print(f"  Cleared {count} labels from json_code")
    assert count == 2
    
    # Test 8: Thread safety (basic check)
    print("\n✓ Test 8: Thread safety")
    import threading
    errors = []
    
    def set_labels():
        try:
            for i in range(10):
                manager.set_label(LLMTask.REVIEW_CODE, f"tab_{i}", f"Label {i}")
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=set_labels) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    if errors:
        print(f"  ✗ Thread safety issues: {errors}")
    else:
        print("  Thread-safe operations completed")
    
    # Clean up
    if config_path.exists():
        config_path.unlink()
    
    print("\n✅ All tests passed!")


if __name__ == "__main__":
    test_button_label_manager()
