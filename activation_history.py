import json
import os
import logging
from typing import Dict, List, Set
from datetime import datetime
from dataclasses import dataclass
import sqlite3
import time

logger = logging.getLogger(__name__)

@dataclass
class Activation:
    activation_id: int
    phone_number: str
    service: str
    timestamp: float
    status: str  # 'completed', 'cancelled', 'refunded'
    port: str

class ActivationHistoryManager:
    def __init__(self, db_path: str = "data/activation_history.db"):
        """Initialize activation history manager with SQLite storage."""
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activations (
                    activation_id INTEGER PRIMARY KEY,
                    phone_number TEXT NOT NULL,
                    service TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    status TEXT NOT NULL,
                    port TEXT NOT NULL
                )
            """)
            
            # Add blocked_services table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocked_services (
                    phone_number TEXT NOT NULL,
                    service TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (phone_number, service)
                )
            """)
            
            # Create indices for faster lookups
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_phone_service 
                ON activations(phone_number, service)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_blocked_services 
                ON blocked_services(phone_number, service)
            """)

    def add_activation(self, activation: Activation):
        """Add new activation to history."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO activations 
                (activation_id, phone_number, service, timestamp, status, port)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                activation.activation_id,
                activation.phone_number,
                activation.service,
                activation.timestamp,
                activation.status,
                activation.port
            ))

    def add_blocked_service(self, phone_number: str, service: str):
        """Add a service to blocked list for a phone number."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO blocked_services 
                (phone_number, service, timestamp)
                VALUES (?, ?, ?)
            """, (phone_number, service, time.time()))

    def is_service_blocked(self, phone_number: str, service: str) -> bool:
        """Check if a service is blocked for a phone number."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM blocked_services 
                WHERE phone_number = ? AND service = ?
            """, (phone_number, service))
            return cursor.fetchone()[0] > 0

    def get_service_count(self, phone_number: str, service: str) -> int:
        """Get number of completed or cancelled activations for a phone/service combination."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM activations 
                WHERE phone_number = ? 
                AND service = ? 
                AND status IN ('completed', 'cancelled')
            """, (phone_number, service))
            return cursor.fetchone()[0]

    def is_service_available(self, phone_number: str, service: str) -> bool:
        """Check if service is still available for phone number."""
        if self.is_service_blocked(phone_number, service):
            return False
        count = self.get_service_count(phone_number, service)
        return count < 4  # Max 4 attempts per service per phone

    def get_available_services(self, phone_number: str) -> Set[str]:
        """Get set of services still available for phone number."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT service, COUNT(*) as count 
                FROM activations 
                WHERE phone_number = ? AND status = 'completed'
                GROUP BY service
                HAVING count >= 4
            """, (phone_number,))
            
            # Get services that have reached limit
            maxed_services = {row[0] for row in cursor.fetchall()}
            
            # Return all services except maxed ones
            all_services = set(config.get('services', {}).keys())
            return all_services - maxed_services

    def update_activation_status(self, activation_id: int, status: str):
        """Update status of an activation."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE activations 
                SET status = ? 
                WHERE activation_id = ?
            """, (status, activation_id)) 