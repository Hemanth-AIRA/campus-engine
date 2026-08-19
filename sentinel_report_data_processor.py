"""
Sentinel Report Data Processor
Extracts and transforms data from MySQL database for report generation
"""
import os
import pymysql
from datetime import datetime, timedelta
import base64
import re
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from sshtunnel import SSHTunnelForwarder
import logging
import requests
from jinja2 import Template, Environment, FileSystemLoader
import os

logger = logging.getLogger(__name__)
import integrations.aira_sql as airadb
    

class SentinelDataProcessor:
    """Process Sentinel monitoring data into report-ready format"""
    
    @staticmethod
    def _normalize_bytea(raw):
        """Normalize PostgreSQL bytea to raw bytes (handles memoryview, hex-encoded, base64 strings, plain bytes)."""
        if raw is None:
            return None
        if isinstance(raw, memoryview):
            return bytes(raw)
        if isinstance(raw, str):
            # Handle hex-encoded bytea (PostgreSQL format)
            if raw.startswith('\\x'):
                return bytes.fromhex(raw[2:])
            # Handle base64-encoded strings (from API responses)
            try:
                return base64.b64decode(raw)
            except Exception:
                return raw.encode('latin-1')
        return bytes(raw)

    def __init__(self):
        """Initialize with environment-based configuration"""
        # EC2 SSH Details
        self.EC2_HOST = os.getenv("EC2_HOST", False)
        self.EC2_USER = os.getenv("EC2_USER", False)
        self.EC2_KEY = os.getenv("EC2_KEY", False)
        
        # MySQL Details
        self.MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
        self.MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
        self.MYSQL_USER = os.getenv("MYSQL_USER")
        self.MYSQL_PASS = os.getenv("MYSQL_PASS")
        self.MYSQL_DB = os.getenv("MYSQL_DB")
        
        self.conn = None
        self.tunnel = None
    
    def connect(self):
        """Establish database connection (with optional SSH tunnel). Skips in API mode."""
        if not airadb._IS_LOCAL:
            logger.info("🔗 API mode — skipping direct DB connection")
            return
        if not self.EC2_HOST:
            logger.info("🔗 EC2_HOST is None/empty - connecting directly to localhost")
            self.conn = pymysql.connect(
                host='localhost',
                port=self.MYSQL_PORT,
                user=self.MYSQL_USER,
                password=self.MYSQL_PASS,
                database=self.MYSQL_DB,
                cursorclass=pymysql.cursors.DictCursor
            )
            self.tunnel = None
        else:
            # Create SSH tunnel
            self.tunnel = SSHTunnelForwarder(
                (self.EC2_HOST, 22),
                ssh_username=self.EC2_USER,
                ssh_pkey=self.EC2_KEY,
                remote_bind_address=(self.MYSQL_HOST, self.MYSQL_PORT),
                local_bind_address=('127.0.0.1', 3307)
            )
            self.tunnel.start()
            logger.info(f"✅ SSH Tunnel established at 127.0.0.1:{self.tunnel.local_bind_port}")
            
            self.conn = pymysql.connect(
                host='localhost',
                port=self.tunnel.local_bind_port,
                user=self.MYSQL_USER,
                password=self.MYSQL_PASS,
                database=self.MYSQL_DB,
                cursorclass=pymysql.cursors.DictCursor
            )
    
    def disconnect(self):
        """Close database connection and tunnel"""
        if self.conn:
            try:
                self.conn.close()
                logger.info("✅ Database connection closed")
            except Exception as e:
                logger.info(f"❌ Error closing database connection: {e}")
        
        if self.tunnel:
            try:
                self.tunnel.stop()
                logger.info("✅ SSH tunnel closed")
            except Exception as e:
                logger.info(f"❌ Error closing SSH tunnel: {e}")
    
    def get_interview_data(self, room_id: str) -> List[Dict]:
        """
        Fetch all events for a specific interview
        
        Args:
            room_id: Interview room ID (e.g., 'AIRA-20251107-1322-649C')
        
        Returns:
            List of event dictionaries
        """
        # ── API mode: call GET /sentinel/events/{roomId} ──
        if not airadb._IS_LOCAL:
            try:
                base_host = airadb._ENGINE_API_BASE.replace('/api/engine', '')
                logger.info(base_host)
                url = f"{base_host}/api/sentinel/events/{room_id}"
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                result = response.json()
                if isinstance(result, list):
                    for event in result:
                        if 'timestamp' in event and isinstance(event['timestamp'], str):
                            try:
                                event['timestamp'] = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                            except ValueError:
                                pass
                    return result
                return []
            except Exception as e:
                logger.error(f"❌ sentinel/events API error: {e}")
                return []

        # ── LOCAL: direct DB (unchanged) ──
        query = """
            SELECT id, room_id, timestamp, type, value, description, snapshot
            FROM sentinel_report
            WHERE room_id = %s
            ORDER BY timestamp ASC
        """
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, (room_id,))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"❌ db execution failed {e}")
            raise
    
    def parse_description(self, description: str) -> Dict:
        """
        Extract structured data from description field
        
        Args:
            description: Raw description text
        
        Returns:
            Dict with extracted metrics
        """
        result = {}
        
        # Extract quality/confidence values
        quality_match = re.search(r'quality[:\s]+(\d+\.?\d*)\s*%?', description, re.IGNORECASE)
        if quality_match:
            quality_val = float(quality_match.group(1))
            # ADD THIS: Normalize to 0-1 scale
            if quality_val > 1.0:  # Assume it's a percentage
                quality_val = quality_val / 100.0
            result['quality'] = quality_val
        
        # Extract center coordinates
        center_match = re.search(r'center[:\s]+(\d+\.?\d*)', description, re.IGNORECASE)
        if center_match:
            result['center'] = float(center_match.group(1))
        
        # Extract duration for absence/presence events
        duration_match = re.search(r'(\d+\.?\d*)\s*s', description)
        if duration_match:
            result['duration_seconds'] = float(duration_match.group(1))
        
        # Extract people count
        people_match = re.search(r'(\d+)\s*people', description, re.IGNORECASE)
        if people_match:
            result['people_count'] = int(people_match.group(1))
        
        # Extract reference path for swap events
        ref_match = re.search(r'ref=([^\s]+)', description)
        if ref_match:
            result['reference_path'] = ref_match.group(1)
        
        return result
    
    def classify_violation(self, event_type: str, value: str, description: str, parsed: Dict, duration: float = 0) -> Tuple[str, str, str]:
        """
        Classify violation severity and details
        
        Returns:
            Tuple of (severity, severity_level, icon)
            - severity: 'critical', 'warning', 'low'
            - severity_level: 'high', 'medium', 'low'
            - icon: emoji for display
        """
        # Identity Swap - ALWAYS CRITICAL
        if event_type == 'Swap' and value == 'detected':
            if duration > 10:  # Long swap
                return ('critical', 'high', '⚠️')
            elif duration > 5:  # Medium swap
                return ('critical', 'high', '⚠️')
            else:  # Short or unknown swap
                return ('critical', 'high', '⚠️')
        
        # Multiple People Detection
        if event_type == 'Multiperson' and value == 'detected':
            people_count = parsed.get('people_count', 2)
            if people_count > 2:
                return ('critical', 'high', '👥')
            else:
                return ('warning', 'medium', '👥')
        
        # Candidate Absence
        if event_type == 'Candidate present' and value == 'false':
            duration = parsed.get('duration_seconds', 0)
            # Extract from description if not in parsed
            if duration == 0 and '>' in description:
                dur_match = re.search(r'(\d+\.?\d*)\s*s', description)
                if dur_match:
                    duration = float(dur_match.group(1))
            
            if duration > 10:
                return ('critical', 'high', '❌')
            elif duration > 5:
                return ('warning', 'medium', '❌')
            else:
                return ('low', 'low', '❌')
        
        # Default for other events
        return ('low', 'low', '📹')
    
    def extract_violations(self, events: List[Dict]) -> Dict:
        """Extract and classify all violation events"""
        violations = []
        critical_count = 0
        total_violation_seconds = 0.0
        
        swap_detected_events = [e for e in events if e['type'] == 'Swap' and e['value'] == 'detected']
        swap_cleared_events = [e for e in events if e['type'] == 'Swap' and e['value'] == 'cleared']
        swap_durations = {}
        for cleared_event in swap_cleared_events:
            parsed = self.parse_description(cleared_event.get('description', ''))
            duration = parsed.get('duration_seconds', 0)
            # Use timestamp as key to match with detected event
            swap_durations[cleared_event['timestamp']] = duration
        multiperson_events = [e for e in events if e['type'] == 'Multiperson' and e['value'] == 'detected']
        absence_events = [e for e in events if e['type'] == 'Candidate present' and e['value'] == 'false']
        
        
        # Process Swap violations - with duration
        for event in swap_detected_events:
            parsed = self.parse_description(event.get('description', ''))
            
            # Find matching cleared event to get duration
            duration = 0.0
            for cleared_time, dur in swap_durations.items():
                if cleared_time > event['timestamp']:
                    duration = dur
                    break
            
            # Add swap duration to total violation time
            total_violation_seconds += duration
            
            severity, severity_level, icon = self.classify_violation(
                'Swap', 'detected', event.get('description', ''), parsed, duration
            )
            
            if severity == 'critical':
                critical_count += 1
            
            image_url = self._get_snapshot_base64(event)
            
            violations.append({
                'timestamp': event['timestamp'].strftime('%b %d, %Y - %H:%M:%S'),
                'event_type': 'Swap',
                'event_type_display': 'Identity Swap Detected',
                'description': 'Different person detected in candidate frame.',
                'severity': severity,
                'severity_level': severity_level,
                'severity_display': severity_level.upper() + ' RISK',
                'icon': icon,
                'confidence': parsed.get('quality', None),
                'duration': f"{duration:.1f} seconds" if duration > 0 else "Unknown",
                'duration_seconds': duration,
                'image_url': image_url
            })
        

        # Process Multiperson violations - CALCULATE DURATION FROM TIMESTAMPS
        for idx, event in enumerate(multiperson_events):
            parsed = self.parse_description(event.get('description', ''))
            people_count = parsed.get('people_count', 2)
            
            # Calculate duration: time until next event in the FULL events list
            duration = 0.0
            current_timestamp = event['timestamp']
            
            # Find the next event after this one in the complete events list
            next_event = None
            for e in events:
                if e['timestamp'] > current_timestamp:
                    next_event = e
                    break
            
            if next_event:
                duration = (next_event['timestamp'] - current_timestamp).total_seconds()
            
            # Add multiperson duration to total violation time
            total_violation_seconds += duration
            
            severity, severity_level, icon = self.classify_violation(
                'Multiperson', 'detected', event.get('description', ''), parsed
            )
            
            if severity == 'critical':
                critical_count += 1
            
            image_url = self._get_snapshot_base64(event)
            
            # Each multiperson event is treated independently with calculated duration
            violations.append({
                'timestamp': event['timestamp'].strftime('%b %d, %Y - %H:%M:%S'),
                'event_type': 'Multiperson',
                'event_type_display': 'Multiple People Detected',
                'description': f'{people_count} people detected in frame.',
                'severity': severity,
                'severity_level': severity_level,
                'severity_display': severity_level.upper() + ' RISK',
                'icon': icon,
                'confidence': parsed.get('quality', None),
                'duration': f"{duration:.1f} seconds",
                'duration_seconds': duration,
                'image_url': image_url
            })
            
        # Process Absence violations - HAS DURATION IN DB
        for event in absence_events:
            parsed = self.parse_description(event.get('description', ''))
            duration = parsed.get('duration_seconds', 0)
            
            severity, severity_level, icon = self.classify_violation(
                'Candidate present', 'false', event.get('description', ''), parsed
            )
            
            if severity == 'critical':
                critical_count += 1
            
            # ADD absence duration to total
            total_violation_seconds += duration
            
            image_url = self._get_snapshot_base64(event)
            
            violations.append({
                'timestamp': event['timestamp'].strftime('%b %d, %Y - %H:%M:%S'),
                'event_type': 'Candidate present',
                'event_type_display': 'Candidate Absent',
                'description': 'Candidate not visible.',
                'severity': severity,
                'severity_level': severity_level,
                'severity_display': severity_level.upper() + ' RISK',
                'icon': icon,
                'confidence': parsed.get('quality', None),
                'duration': f"{duration:.1f} seconds",
                'duration_seconds': duration,
                'image_url': image_url
            })
    
        # Sort all violations by timestamp
        violations.sort(key=lambda x: x['timestamp'])
        
        high_risk_count = sum(1 for v in violations if v['severity'] == 'critical')
        low_risk_count = len(violations) - high_risk_count

        return {
            'has_violations': len(violations) > 0,
            'total_count': len(violations),
            'critical_count': critical_count,
            'high_risk_count': high_risk_count,
            'low_risk_count': low_risk_count,
            'total_violation_seconds': total_violation_seconds,
            'events': violations
        }

    # ADD helper methods:
    def _group_multiperson_incidents(self, events: List[Dict]) -> List[Dict]:
        """Group multiperson events into incidents (same as calculate_multiperson_metrics)"""
        multiperson_events = [
            e for e in events
            if e['type'] == 'Multiperson' and e['value'] == 'detected'
        ]
        
        if not multiperson_events:
            return []
        
        incidents = []
        current_incident = None
        
        for event in multiperson_events:
            parsed = self.parse_description(event.get('description', ''))
            people_count = parsed.get('people_count', 2)
            timestamp = event['timestamp']
            
            if current_incident is None:
                current_incident = {
                    'start_time': timestamp,
                    'end_time': timestamp,
                    'people_count': people_count,
                    'events': [event]
                }
            else:
                time_gap = (timestamp - current_incident['end_time']).total_seconds()
                
                if time_gap <= 10 and people_count == current_incident['people_count']:
                    current_incident['end_time'] = timestamp
                    current_incident['events'].append(event)
                else:
                    incidents.append(current_incident)
                    current_incident = {
                        'start_time': timestamp,
                        'end_time': timestamp,
                        'people_count': people_count,
                        'events': [event]
                    }
        
        if current_incident:
            incidents.append(current_incident)
        
        return incidents

    def _get_snapshot_base64(self, event: Dict) -> Optional[str]:
        """Convert snapshot (bytea) to base64 data URL"""
        if event.get('snapshot'):
            try:
                bytea_data = self._normalize_bytea(event['snapshot'])
                if bytea_data:
                    img_base64 = base64.b64encode(bytea_data).decode('utf-8')
                    return f"data:image/jpeg;base64,{img_base64}"
            except:
                pass
        return None
    
    def get_logo_base64(self, logo_path: str = None) -> Optional[str]:
        """
        Convert logo image to base64 for embedding in HTML
        
        Args:
            logo_path: Path to logo image file (PNG, JPG, or SVG)
        
        Returns:
            Base64 encoded data URL or None if file not found
        """
        if not logo_path:
            # Default logo path - adjust to your actual path
            logo_path = os.path.join(os.path.dirname(__file__), 'interview_reports', 'AIRA_LOGO.jpg')
        
        if not os.path.exists(logo_path):
            logger.info(f"⚠️ Logo file not found at: {logo_path}")
            return None
        
        try:
            with open(logo_path, 'rb') as f:
                logo_data = f.read()
                logo_base64 = base64.b64encode(logo_data).decode('utf-8')
                
                # Detect image type from file extension
                ext = logo_path.lower().split('.')[-1]
                if ext == 'png':
                    mime_type = 'image/png'
                elif ext in ['jpg', 'jpeg']:
                    mime_type = 'image/jpeg'
                elif ext == 'svg':
                    mime_type = 'image/svg+xml'
                else:
                    mime_type = 'image/png'  # default
                
                return f"data:{mime_type};base64,{logo_base64}"
        except Exception as e:
            logger.info(f"❌ Error loading logo: {e}")
            return None
        
    def calculate_identity_metrics(self, events: List[Dict]) -> Dict:
        """Calculate identity verification metrics with swap duration"""
        swap_detected = [e for e in events if e['type'] == 'Swap' and e['value'] == 'detected']
        swap_cleared = [e for e in events if e['type'] == 'Swap' and e['value'] == 'cleared']
        
        # Calculate total swap time and longest swap
        total_swap_time = 0.0
        longest_swap = 0.0
        
        for cleared_event in swap_cleared:
            parsed = self.parse_description(cleared_event.get('description', ''))
            duration = parsed.get('duration_seconds', 0)
            total_swap_time += duration
            longest_swap = max(longest_swap, duration)
        
        reference_events = [e for e in events if e['type'] == 'Reference' and e['value'] == 'enrolled']
        best_events = [e for e in events if e['type'] == 'Best']
        
        qualities = []
        for event in reference_events + best_events:
            parsed = self.parse_description(event.get('description', ''))
            if 'quality' in parsed:
                qualities.append(parsed['quality'])
        
        avg_confidence = int(sum(qualities) / len(qualities) * 100) if qualities else 0
        face_changes = len(swap_detected)
        
        return {
            'face_changes': face_changes,
            'avg_similarity': avg_confidence,
            'total_swap_time': f"{total_swap_time:.1f}",
            'longest_swap': f"{longest_swap:.1f}",
            'low_confidence_periods': len([q for q in qualities if q < 0.5]),
            'summary': f'{face_changes} identity swap(s) detected' if face_changes > 0 else 'No identity swaps detected'
        }

    def calculate_presence_metrics(self, events: List[Dict]) -> Dict:
        """Calculate presence monitoring metrics - NO SCORING"""
        absent_events = [
            e for e in events 
            if e['type'] == 'Candidate present' and e['value'] == 'false'
        ]
        
        total_absence_seconds = 0
        longest_absence = 0
        
        for event in absent_events:
            parsed = self.parse_description(event.get('description', ''))
            duration = parsed.get('duration_seconds', 0)
            
            if duration == 0 and event.get('description'):
                dur_match = re.search(r'(\d+\.?\d*)\s*s', event['description'])
                if dur_match:
                    duration = float(dur_match.group(1))
            
            total_absence_seconds += duration
            longest_absence = max(longest_absence, duration)
        
        if len(events) >= 2:
            start_time = events[0]['timestamp']
            end_time = events[-1]['timestamp']
            total_seconds = (end_time - start_time).total_seconds()
        else:
            total_seconds = 0
        
        time_present_pct = 100
        if total_seconds > 0:
            time_present_pct = int((1 - total_absence_seconds / total_seconds) * 100)
        
        return {
            'time_present': time_present_pct,
            'absence_count': len(absent_events),
            'total_absence_seconds': total_absence_seconds,
            'longest_absence': f"{longest_absence:.1f}",
            'summary': f'{len(absent_events)} absence event(s) detected'
        }

    def calculate_multiperson_metrics(self, events: List[Dict]) -> Dict:
        """Calculate multi-person detection metrics with proper duration tracking"""
        multiperson_events = [
            e for e in events
            if e['type'] == 'Multiperson' and e['value'] == 'detected'
        ]
        
        if not multiperson_events:
            return {
                'event_count': 0,
                'max_people': 1,
                'total_duration': '0.0',
                'longest_duration': '0.0',
                'incidents_detail': [],
                'summary': 'No multi-person incidents detected'
            }
        
        # Track durations and max people
        max_people = 1
        total_duration_seconds = 0.0
        longest_duration_seconds = 0.0
        
        for idx, event in enumerate(multiperson_events):
            parsed = self.parse_description(event.get('description', ''))
            people_count = parsed.get('people_count', 2)
            max_people = max(max_people, people_count)
            
            # Calculate duration: time until next event in the FULL events list
            duration = 0.0
            current_timestamp = event['timestamp']
            
            # Find the next event after this one in the complete events list
            next_event = None
            for e in events:
                if e['timestamp'] > current_timestamp:
                    next_event = e
                    break
            
            if next_event:
                duration = (next_event['timestamp'] - current_timestamp).total_seconds()
            
            # Accumulate totals
            total_duration_seconds += duration
            longest_duration_seconds = max(longest_duration_seconds, duration)
        
        return {
            'event_count': len(multiperson_events),
            'max_people': max_people,
            'total_duration': f"{total_duration_seconds:.1f}",
            'longest_duration': f"{longest_duration_seconds:.1f}",
            'incidents_detail': [],
            'summary': f'{len(multiperson_events)} multi-person detection(s)'
        }
    
    def calculate_overall_score(self, events: List[Dict], violations: Dict) -> Dict:
        """Calculate overall security score based on violation time percentage"""
        
        # Calculate total session duration
        if len(events) >= 2:
            start_time = events[0]['timestamp']
            end_time = events[-1]['timestamp']
            total_seconds = (end_time - start_time).total_seconds()
        else:
            total_seconds = 0
        
        # Get total violation time (only from absence events currently)
        total_violation_seconds = violations.get('total_violation_seconds', 0.0)
        
        # Calculate violation percentage
        if total_seconds > 0:
            violation_percentage = (total_violation_seconds / total_seconds) * 100
        else:
            violation_percentage = 0
        
        # Determine status based on violation percentage
        if violation_percentage == 0:
            status = 'clean'
            alert_type = 'success'
            color_grade = 'clean'
            status_text = 'CLEAN SESSION - NO VIOLATIONS'
            summary = 'Excellent interview conduct detected throughout the session. The candidate maintained proper interview etiquette without any security violations.'
        elif violation_percentage < 11:
            status = 'warning'
            alert_type = 'warning'
            color_grade = 'warning'
            status_text = 'MODERATE VIOLATIONS DETECTED'
            summary = f'Security violations detected for {violation_percentage:.1f}% of session duration ({total_violation_seconds:.0f}s out of {total_seconds:.0f}s). Review recommended.'
        else:
            status = 'critical'
            alert_type = 'critical'
            color_grade = 'critical'
            status_text = 'CRITICAL VIOLATIONS DETECTED'
            summary = f'Severe violations comprising {violation_percentage:.1f}% of session time ({total_violation_seconds:.0f}s out of {total_seconds:.0f}s). Immediate review required.'
        
        return {
            'violation_percentage': round(violation_percentage, 1),
            'total_session_seconds': total_seconds,
            'total_violation_seconds': total_violation_seconds,
            'status': status,
            'alert_type': alert_type,
            'color_grade': color_grade,  # NEW: For template coloring
            'status_text': status_text,
            'summary': summary
        }

    def calculate_violations_severity(self, violations: Dict) -> Dict:
        """Determine severity level for violations section based on high-risk count"""
        high_risk_count = violations.get('high_risk_count', 0)
        total_count = violations.get('total_count', 0)
        
        if total_count == 0:
            return {
                'color_grade': 'clean',
                'alert_type': 'success',
                'violation_message': 'No security violations detected.'
            }
        elif high_risk_count > 0:  # ANY high-risk = CRITICAL (RED)
            return {
                'color_grade': 'critical',
                'alert_type': 'critical',
                'violation_message': f'{high_risk_count} high-severity security violation(s) detected. Immediate review recommended.'
            }
        else:  # Only low/medium risk = WARNING (ORANGE)
            return {
                'color_grade': 'warning',
                'alert_type': 'warning',
                'violation_message': f'{total_count} security violation(s) detected. Review recommended.'
            }
        
    def extract_candidate_info(self, events: List[Dict], room_id: str) -> Dict:
        """Extract candidate information from database using room_id"""
        # Try to get enrolled face snapshot
        enrolled_face_url = None
        enrollment_event = next(
            (e for e in events if e['type'] == 'Reference' and e['value'] == 'enrolled'),
            None
        )
        if enrollment_event and enrollment_event.get('snapshot'):
            try:
                bytea_data = self._normalize_bytea(enrollment_event['snapshot'])
                if bytea_data:
                    img_base64 = base64.b64encode(bytea_data).decode('utf-8')
                    enrolled_face_url = f"data:image/jpeg;base64,{img_base64}"
            except:
                pass

        # Try to get BEST face snapshot (preferred)
        best_event = next(
            (e for e in events if e['type'] == 'Best'),
            None
        )
        
        if best_event and best_event.get('snapshot'):
            try:
                bytea_data = self._normalize_bytea(best_event['snapshot'])
                if bytea_data:
                    img_base64 = base64.b64encode(bytea_data).decode('utf-8')
                    enrolled_face_url = f"data:image/jpeg;base64,{img_base64}"  # Override with Best
            except:
                pass
            
        # Fetch candidate info from interviews table using room_id
        candidate_name = 'Candidate'
        candidate_email = 'candidate@example.com'
        
        try:
            # ── API mode: use sentinel API data ──
            if not airadb._IS_LOCAL:
                # Sentinel events already contain candidate_name/email
                base_host = airadb._ENGINE_API_BASE.replace('/api/engine', '')
                url = f"{base_host}/api/sentinel/events/{room_id}"
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    sentinel_result = response.json()
                else:
                    sentinel_result = []
                
                if sentinel_result and isinstance(sentinel_result, list) and len(sentinel_result) > 0:
                    first = sentinel_result[0]
                    candidate_name = first.get('candidate_name') or first.get('candidateName') or 'Candidate'
                    candidate_email = first.get('candidate_email') or first.get('candidateEmail') or 'candidate@example.com'
            else:
                # ── LOCAL: direct DB (unchanged) ──
                query = """
                    SELECT candidate_name, candidate_email
                    FROM Sentinel_report
                    WHERE room_id = %s
                    LIMIT 1
                """
                with self.conn.cursor() as cursor:
                    cursor.execute(query, (room_id,))
                    result = cursor.fetchone()
                    
                    if result:
                        candidate_name = result.get('candidate_name', 'Candidate')
                        candidate_email = result.get('candidate_email', 'candidate@example.com')
        except Exception as e:
            logger.info(f"⚠️ Could not fetch candidate info from database: {e}")
            # Fall back to defaults

        has_enrolled_face = any(
            e for e in events 
            if e['type'] == 'Reference' and e['value'] == 'enrolled' and e.get('snapshot')
        )

        if has_enrolled_face:
            enrollment_status = 'secure'
            enrollment_text = 'ENROLLED'
        else:
            enrollment_status = 'not-enrolled'
            enrollment_text = 'NOT ENROLLED'

        return {
            'name': candidate_name,
            'email': candidate_email,
            'enrolled_face_url': enrolled_face_url,
            'enrollment_status': enrollment_status,
            'enrollment_text': enrollment_text
        }
    
    def extract_session_info(self, events: List[Dict], room_id: str) -> Dict:
        """Extract session metadata"""
        start_event = next((e for e in events if e['type'] == 'Start'), None)
        end_event = next((e for e in events if e['type'] == 'Session' and e['value'] == 'ended'), None)
        
        if start_event and end_event:
            duration = end_event['timestamp'] - start_event['timestamp']
            duration_str = f"{int(duration.total_seconds() // 60)}:{int(duration.total_seconds() % 60):02d}"
        else:
            duration_str = "N/A"
        
        datetime_str = start_event['timestamp'].strftime('%B %d, %Y %I:%M %p UTC') if start_event else 'N/A'
        
        # Extract platform from description
        platform = 'Video Platform'
        if start_event and start_event.get('description'):
            if 'Playwright' in start_event['description']:
                platform = 'Google Meet'
            # Add more platform detection logic
        
        return {
            'interview_id': room_id,
            'datetime': datetime_str,
            'platform': platform,
            'recording_status': 'secure',
            'recording_text': 'ENABLED',
            'monitoring_status': 'secure',
            'monitoring_text': 'ACTIVE'
        }
    
    def extract_evidence_snapshots(self, events: List[Dict]) -> Dict:
        """Extract evidence snapshots - ALL swaps, multiperson, enrolled, best, longest absence"""
        
        snapshot_events = []
        
        # 1. Enrolled face (always first)
        enrolled = next((e for e in events if e['type'] == 'Reference' and e['value'] == 'enrolled'), None)
        if enrolled and enrolled.get('snapshot'):
            snapshot_events.append(('Enrolled Face', enrolled))
        
        # 2. Best quality frame
        best = next((e for e in events if e['type'] == 'Best'), None)
        if best and best.get('snapshot'):
            snapshot_events.append(('Best Quality Frame', best))
        
        # 3. ALL Swap events (not just one)
        swap_events = [e for e in events if e['type'] == 'Swap' and e['value'] == 'detected' and e.get('snapshot')]
        for swap in swap_events:
            snapshot_events.append(('Identity Swap', swap))
        
        # 4. Multiperson event (max people count)
        multiperson_events = [e for e in events if e['type'] == 'Multiperson' and e['value'] == 'detected' and e.get('snapshot')]
        if multiperson_events:
            max_people_event = max(multiperson_events, 
                                key=lambda x: self.parse_description(x.get('description', '')).get('people_count', 0))
            snapshot_events.append(('Multiple People', max_people_event))
        
        # 5. Absence event (longest duration) - label as "Candidate Absent"
        absence_events = [e for e in events if e['type'] == 'Candidate present' and e['value'] == 'false' and e.get('snapshot')]
        if absence_events:
            longest_absence = max(absence_events,
                                key=lambda x: self.parse_description(x.get('description', '')).get('duration_seconds', 0))
            snapshot_events.append(('Candidate Absent', longest_absence))
        
        # Process snapshots
        snapshots = []
        for type_display, event in snapshot_events[:9]:  # Allow up to 9 images
            parsed = self.parse_description(event.get('description', ''))
            
            try:
                bytea_data = self._normalize_bytea(event['snapshot'])
                if bytea_data:
                    img_base64 = base64.b64encode(bytea_data).decode('utf-8')
                    image_url = f"data:image/jpeg;base64,{img_base64}"
                else:
                    image_url = None
            except:
                image_url = None
            
            confidence = None
            if 'quality' in parsed:
                confidence = int(parsed['quality'] * 100)
            
            snapshots.append({
                'type_display': type_display,  # Now uses the exact label we want
                'timestamp': event['timestamp'].strftime('%H:%M:%S'),
                'confidence': confidence,
                'image_url': image_url
            })
        
        # Calculate duration
        if len(events) >= 2:
            duration = events[-1]['timestamp'] - events[0]['timestamp']
            duration_str = f"{int(duration.total_seconds() // 60)}:{int(duration.total_seconds() % 60):02d}"
        else:
            duration_str = "N/A"
        
        return {
            'has_evidence': len(snapshots) > 0,
            'total_snapshots': len(snapshot_events),
            'video_duration': duration_str,
            'snapshots': snapshots
        }
    
    def generate_recommendations(self, violations: Dict, overall: Dict) -> Dict:
        """Generate recommendations based on findings"""
        security_concerns = []
        violation_pct = overall['violation_percentage']
        
        if violations['has_violations']:
            # Group critical violations by type
            grouped_violations = {}
            
            for violation in violations['events']:
                if violation['severity'] == 'critical':
                    event_type = violation['event_type_display']
                    
                    if event_type not in grouped_violations:
                        grouped_violations[event_type] = []
                    
                    # Extract just the time portion (HH:MM:SS)
                    time_only = violation['timestamp'].split(' - ')[1] if ' - ' in violation['timestamp'] else violation['timestamp']
                    grouped_violations[event_type].append(time_only)
            
            # Format grouped violations
            for event_type, timestamps in grouped_violations.items():
                if len(timestamps) == 1:
                    # Single occurrence - show as before
                    security_concerns.append(
                        f"Critical {event_type.lower()} at {timestamps[0]}"
                    )
                else:
                    # Multiple occurrences - show ALL timestamps
                    timestamp_str = ", ".join(timestamps)
                    security_concerns.append(
                        f"Critical {event_type.lower()} detected {len(timestamps)} times: {timestamp_str}"
                    )

        # Base assessment on violation percentage
        if violation_pct == 0:
            validity_type = 'success'
            validity_text = 'EXCELLENT INTEGRITY: This interview session demonstrates exemplary conduct with no security violations detected.'
            next_steps = [
                'Proceed with normal interview evaluation',
                'No additional verification required',
                'Archive according to standard retention policy'
            ]
        elif violation_pct < 11:
            validity_type = 'warning'
            validity_text = f'MODERATE CONCERNS: Violations detected for {violation_pct:.1f}% of session duration. Review recommended before assessment.'
            next_steps = [
                'Review all flagged security events',
                'Consider follow-up verification call',
                'Document findings in candidate file'
            ]
        else:
            validity_type = 'critical'
            validity_text = f'CRITICAL ISSUES: {violation_pct:.1f}% of session compromised by violations. Immediate review required.'
            next_steps = [
                'Schedule immediate review meeting',
                'Conduct secondary interview with enhanced monitoring',
                'Consider stricter technical requirements'
            ]
        
        return {
            'security_concerns': security_concerns,
            'interview_validity': validity_text,
            'validity_type': validity_type,
            'next_steps': next_steps
        }

    def process_interview(self, room_id: str, logo_path: str = None) -> Dict:
        """Main processing function"""
        try:
            self.connect()
            events = self.get_interview_data(room_id)
            
            if not events:
                raise ValueError(f"No data found for room_id: {room_id}")
            
            # Extract violations FIRST
            violations = self.extract_violations(events)
            
            # Calculate overall score using violation time percentage
            overall_security = self.calculate_overall_score(events, violations)
            
            # Simplified behavioral metrics (no scoring)
            identity_metrics = self.calculate_identity_metrics(events)
            presence_metrics = self.calculate_presence_metrics(events)
            multiperson_metrics = self.calculate_multiperson_metrics(events)
            
            candidate_info = self.extract_candidate_info(events, room_id)
            session_info = self.extract_session_info(events, room_id)
            evidence = self.extract_evidence_snapshots(events)
            recommendations = self.generate_recommendations(violations, overall_security)
            
            # Session stats - now based on time percentage
            total_violations = violations['total_count']
            violation_pct = overall_security['violation_percentage']
            
            if violation_pct == 0:
                violations_severity = 'good'
            elif violation_pct < 11:
                violations_severity = 'warning'
            else:
                violations_severity = 'danger'

            violations_severity_info = self.calculate_violations_severity(violations)

            report_data = {
                'header': {
                    'title': 'AIRA Sentinel Security Analysis Report',
                    'subtitle': f"AI-Powered Interview Integrity Assessment • Interview ID: #{session_info['interview_id']} • Generated {datetime.now().strftime('%B %d, %Y')}",
                    'logo_url': self.get_logo_base64(logo_path)  
                },
                'overall_security': overall_security,  # TIME-BASED (for overall summary)
                'violations_severity': violations_severity_info,  # NEW: COUNT-BASED (for violations section)
                'session_stats': {
                    'total_violations': total_violations,
                    'violations_severity': violations_severity,
                    'violation_percentage': violation_pct,
                    'violation_time': f"{overall_security['total_violation_seconds']:.0f}s",
                    'session_duration': f"{overall_security['total_session_seconds']:.0f}s",
                    'duration': self._format_duration(events)
                },
                'candidate': candidate_info,
                'session': session_info,
                'violations': violations,
                'identity_analysis': identity_metrics,
                'presence_analysis': presence_metrics,
                'multi_person_analysis': multiperson_metrics,
                'evidence': evidence,
                'recommendations': recommendations,
                'footer': {
                    'disclaimer': 'This report contains confidential and proprietary information.',
                    'confidentiality': 'All AI detection results should be verified by human reviewers.'
                }
            }
            
            return report_data
            
        finally:
            self.disconnect()
    
    def _format_duration(self, events: List[Dict]) -> str:
        """Format session duration"""
        if len(events) >= 2:
            duration = events[-1]['timestamp'] - events[0]['timestamp']
            return f"{int(duration.total_seconds() // 60)}:{int(duration.total_seconds() % 60):02d}"
        return "N/A"
    
    def render_html_report(self, report_data: Dict, template_path: str = None) -> str:
        """
        Render HTML report using Jinja2 template
        
        Args:
            report_data: Dictionary from process_interview()
            template_path: Path to Jinja2 template file (default: looks for sentinel_report_template.html.j2)
        
        Returns:
            Complete HTML string
        """
        # Default template path
        if not template_path:
            template_path = os.path.join(os.path.dirname(__file__), 'templates', 'sentinel_report_template.html.j2')
        
        # Check if template exists
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Template not found at: {template_path}")
        
        # Load and render template
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        
        template = Template(template_content)
        html = template.render(**report_data)
        
        return html

    

def start_generation(room_id: str, template_path: str = None, output_path: str = None) -> str:
    """
    Generate complete HTML report for an interview
    
    Args:
        room_id: Interview room ID (e.g., 'AIRA-20251107-1322-649C')
        template_path: Optional path to custom Jinja2 template
        output_path: Optional path to save HTML file (if None, returns HTML string)
    
    Returns:
        Complete HTML string
    """
    processor = SentinelDataProcessor()
    try:
        # Process interview data
        report_data = processor.process_interview(room_id)
        logger.info(f"✅ Successfully processed interview: {room_id}")
        logger.info(f"Overall violation %: {report_data['overall_security']['violation_percentage']}%")
        logger.info(f"Violations: {report_data['violations']['total_count']}")
        
        # Generate HTML
        html = processor.render_html_report(report_data, template_path)
        logger.info(f"✅ HTML report generated successfully")
        
        # Optionally save to file
        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html)
            logger.info(f"✅ HTML report saved to: {output_path}")
        
        return html
        
    except Exception as e:
        logger.error(f"❌ Error generating report: {e}")
        raise

# # Usage Example
# # if __name__ == "__main__":
#     # Initialize processor (reads from environment variables)
#     processor = SentinelDataProcessor()
    
#     # Process interview
#     room_id = 'AIRA-20251107-1322-649C'
    
#     try:
#         report_data = processor.process_interview(room_id)
#         logger.info(f"✅ Successfully processed interview: {room_id}")
#         logger.info(f"Overall violation %: {report_data['overall_security']['violation_percentage']}%")
#         logger.info(f"Violations: {report_data['violations']['total_count']}")
#     except Exception as e:
#         logger.info(f"❌ Error processing interview: {e}")
