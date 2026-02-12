"""
Chat History Manager - Stores and retrieves LLM chat history.

Manages persistent chat history with full prompt/response pairs.
"""

import json
import os
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict


@dataclass
class ChatMessage:
    """A single chat message with full context."""
    role: str  # 'user', 'assistant', 'system'
    content: str  # What's shown in the UI
    full_prompt: Optional[str] = None  # Full prompt sent to LLM (for user messages)
    full_response: Optional[str] = None  # Full raw response from LLM (for assistant messages)
    timestamp: str = ""
    msg_id: str = ""  # Unique message ID (UUID)
    prompt_tokens: int = 0  # Number of tokens in prompt
    completion_tokens: int = 0  # Number of tokens in completion
    total_tokens: int = 0  # Total tokens used
    metadata: Optional[Dict[str, Any]] = None  # Additional metadata (validation issues, etc.)
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())


class ChatHistoryManager:
    """
    Manages persistent chat history for LLM conversations.
    
    Features:
    - Stores messages with full prompt/response for debugging
    - Auto-rotates to keep last 50 messages
    - Creates file on first message (lazy initialization)
    """
    
    MAX_MESSAGES = 50
    FILENAME = ".llm_chat_history.json"
    
    def __init__(self, test_folder: Optional[str] = None):
        """
        Initialize the chat history manager.
        
        Args:
            test_folder: Path to the test folder. History file will be stored here.
        """
        self._test_folder = test_folder
        self._messages: List[ChatMessage] = []
        self._loaded = False
    
    @property
    def file_path(self) -> Optional[str]:
        """Get the path to the history file."""
        if not self._test_folder:
            return None
        return os.path.join(self._test_folder, self.FILENAME)
    
    def set_test_folder(self, folder: str):
        """Set the test folder and load existing history."""
        self._test_folder = folder
        self._loaded = False
        self._messages = []
        self._load()
    
    def _load(self):
        """Load history from file if it exists."""
        if self._loaded or not self.file_path:
            return
        
        self._loaded = True
        
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._messages = [
                        ChatMessage(**msg) for msg in data.get('messages', [])
                    ]
            except (json.JSONDecodeError, OSError, TypeError) as e:
                # Corrupted file, start fresh
                self._messages = []
    
    def _save(self):
        """Save history to file."""
        if not self.file_path:
            return
        
        try:
            data = {
                'version': 1,
                'messages': [asdict(msg) for msg in self._messages]
            }
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass  # Silently fail on write errors
    
    def add_message(
        self,
        role: str,
        content: str,
        full_prompt: Optional[str] = None,
        full_response: Optional[str] = None
    ) -> str:
        """
        Add a message to the history.
        
        Args:
            role: 'user', 'assistant', or 'system'
            content: Text shown in UI
            full_prompt: Full prompt sent to LLM (for user messages)
            full_response: Full raw response from LLM (for assistant messages)
        
        Returns:
            Unique message ID (UUID string)
        """
        self._load()  # Lazy load on first access
        
        msg = ChatMessage(
            role=role,
            content=content,
            full_prompt=full_prompt,
            full_response=full_response
        )
        self._messages.append(msg)
        
        # Rotate if over limit
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages = self._messages[-self.MAX_MESSAGES:]
        
        self._save()
        return msg.msg_id
    
    def get_message(self, index: int) -> Optional[ChatMessage]:
        """Get a message by index."""
        self._load()
        if 0 <= index < len(self._messages):
            return self._messages[index]
        return None
    
    def get_message_by_id(self, msg_id: str) -> Optional[ChatMessage]:
        """Get a message by its unique ID (survives rotation)."""
        self._load()
        return next((m for m in self._messages if m.msg_id == msg_id), None)
    
    def get_all_messages(self) -> List[ChatMessage]:
        """Get all messages."""
        self._load()
        return self._messages.copy()
    
    def clear(self):
        """Clear all messages."""
        self._messages = []
        self._save()
    
    def __len__(self) -> int:
        """Get number of messages."""
        self._load()
        return len(self._messages)
