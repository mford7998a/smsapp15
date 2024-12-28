import logging
import time
from typing import Dict, Optional, List
from flask import Flask, request, jsonify
from config import config
from tunnel_manager import TunnelManager
from setup_localtonet import ensure_localtonet_setup
import threading
from datetime import datetime
from flask_cors import CORS
from flask_compress import Compress
import os
import json
#from api_logger import APILogger

# Remove duplicate logging initialization, just get the logger
logger = logging.getLogger(__name__)

class SmsHubServer:
    def __init__(self):
        """Initialize SMS Hub server."""
        self.app = Flask(__name__)
        self.modems = {}
        self.active_numbers = {}
        self.completed_activations = {}
        self.activation_log_file = 'activations.log'
        self.stats = {
            'total_activations': 0,
            'completed_activations': 0,
            'cancelled_activations': 0,
            'refunded_activations': 0,
            'total_earnings': 0.0,
            'service_stats': {},
            'activation_times': []
        }
        
        # Initialize SMS Hub client
        try:
            from smshub_api import SmsHubAPI, SmsHubConfig
            api_config = SmsHubConfig(
                api_key=config.get('smshub_api_key'),
                agent_id=config.get('smshub_agent_id'),
                server_url=config.get('server_url'),
                api_url="https://agent.unerio.com/agent/api/sms"  # From docs
            )
            self.smshub = SmsHubAPI(api_config)
            # Also initialize the integration with the same API key
            from smshub_integration import SmsHubIntegration
            self.smshub_integration = SmsHubIntegration(
                base_url="https://agent.unerio.com/agent/api/sms",  # Exact URL from docs
                api_key="15431U1ea5e5b53572512438b03fbe8f96fa10"  # Hardcoded API key
            )
            logger.info("SMS Hub integration initialized with URL: https://agent.unerio.com/agent/api/sms")
            logger.info("SMS Hub client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize SMS Hub client: {e}")
            self.smshub = None
            self.smshub_integration = None
        
        # Initialize services
        self.services = config.get('services', {})  # Get services from config.json
        
        # Initialize activation logger
        try:
            from activation_logger import ActivationLogger
            self.activation_logger = ActivationLogger()
            logger.info("Activation logger initialized")
        except ImportError:
            logger.warning("ActivationLogger module not found, logging will be limited")
            self.activation_logger = None
        except Exception as e:
            logger.error(f"Error initializing activation logger: {e}")
            self.activation_logger = None
        
        # Service name mapping
        self.service_map = {
            'whatsapp': 'wa',
            'telegram': 'tg',
            'viber': 'vi',
            'facebook': 'fb',
            'instagram': 'ig',
            'twitter': 'tw',
            'uber': 'ub',
            'gmail': 'gm',
            'yahoo': 'ya',
            'microsoft': 'mc',
            'amazon': 'am',
            'netflix': 'nf',
            'spotify': 'sf',
            'tinder': 'tn'
        }
        
        # Initialize routes
        self._init_routes()
        
    def _init_routes(self):
        """Initialize Flask routes."""
        self.app.route('/', methods=['POST'])(self.handle_request)
        
    def handle_request(self):
        """Handle incoming requests."""
        try:
            data = request.get_json()
            action = data.get('action')
            
            if not action:
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Missing action'
                })
            
            # Route to appropriate handler
            if action == 'GET_SERVICES':
                return self.handle_get_services(data)
            elif action == 'GET_NUMBER':
                return self.handle_get_number(data)
            elif action == 'FINISH_ACTIVATION':
                return self.handle_finish_activation(data)
            else:
                return jsonify({
                    'status': 'ERROR',
                    'error': f'Unknown action: {action}'
                })
                
        except Exception as e:
            logger.error(f"Error handling request: {e}", exc_info=True)
            return jsonify({
                'status': 'ERROR',
                'error': 'Internal Server Error'
            })
            
    def handle_get_number(self, data):
        """Handle GET_NUMBER request."""
        try:
            # Validate required fields
            service = data.get('service')
            if not service:
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Missing service'
                })
                
            # Store original service code exactly as received
            service_code = service  # Don't modify the service code
                
            # Check if service is enabled in config
            if not config.get('services', {}).get(service_code, False):
                logger.error(f"Service not enabled in config: {service_code}")
                return jsonify({
                    'status': 'ERROR',
                    'error': f'Service not available: {service_code}'
                })
                
            # Handle exception phone set
            exception_phones = data.get('exceptionPhoneSet', [])
            
            # Find available number
            available_port = None
            available_phone = None
            
            # Iterate through modems using port as key
            for port, modem in self.modems.items():
                if modem['status'] != 'active':
                    continue
                    
                phone = modem.get('phone', '')
                if not phone or phone == 'Unknown':
                    continue
                    
                # Clean phone number for comparison (remove '+' and any spaces)
                clean_phone = phone.lstrip('+').replace(' ', '')
                
                # Check if number is in exception list
                is_excluded = False
                for prefix in exception_phones:
                    if clean_phone.startswith(str(prefix)):
                        is_excluded = True
                        break
                        
                if is_excluded:
                    continue
                    
                available_port = port
                available_phone = phone
                break
                
            if not available_port or not available_phone:
                return jsonify({
                    'status': 'NO_NUMBERS'
                })
                
            # Generate activation ID
            activation_id = int(time.time() * 1000)
            
            # Update modem status
            self.modems[available_port]['status'] = 'busy'
            self.modems[available_port]['activation_id'] = activation_id
            
            # Store activation with original service code
            self.active_numbers[available_phone] = {
                'activation_id': activation_id,
                'service': service_code,  # Store exact service code from request
                'timestamp': time.time(),
                'sum': data.get('sum', 0),
                'port': available_port  # Store port for reference
            }
            
            # Update stats
            self.stats['total_activations'] += 1
            
            # Return phone number without + prefix as required by API
            return jsonify({
                'status': 'SUCCESS',
                'number': available_phone.lstrip('+'),
                'activationId': activation_id
            })
            
        except Exception as e:
            logger.error(f"Error in get_number: {e}", exc_info=True)
            return jsonify({
                'status': 'ERROR',
                'error': 'Internal Server Error'
            })

    def handle_finish_activation(self, data):
        """Handle FINISH_ACTIVATION request."""
        try:
            activation_id = data.get('activationId')
            status = data.get('status')
            
            if not activation_id or status is None:
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Missing required fields'
                })
            
            # Find the activation
            phone = None
            port = None
            activation = None
            
            for num, details in self.active_numbers.items():
                if details.get('activation_id') == activation_id:
                    phone = num
                    activation = details
                    port = details.get('port')
                    break
                    
            if not activation:
                # If no active activation but phone exists, it was likely already completed
                return jsonify({'status': 'SUCCESS'})

            # Log status change
            self.log_activation_status(activation_id, status, phone)

            # Update activation status based on status code
            if status == 1:  # Service blocked for this number
                logger.info(f"Service blocked: {phone} - {activation['service']}")
                # Add to blocked services
                self.activation_history.add_blocked_service(phone, activation['service'])
                
                # Update service stats
                service = activation['service']
                if service not in self.stats['service_stats']:
                    self.stats['service_stats'][service] = {'completed': 0, 'cancelled': 0, 'refunded': 0, 'blocked': 0}
                self.stats['service_stats'][service]['blocked'] += 1
                
                # Clean up
                self.active_numbers.pop(phone, None)
                if port and port in self.modems:
                    self.modems[port]['status'] = 'active'
                    self.modems[port].pop('activation_id', None)
                    
            elif status == 3:  # Successfully completed
                self.save_activation(phone, activation['service'], 'completed')
                self.stats['completed_activations'] += 1
                self.stats['total_earnings'] += float(activation.get('sum', 0))
                logger.info(f"Activation completed: {phone} - {activation['service']}")
                
                # Update service stats
                service = activation['service']
                if service not in self.stats['service_stats']:
                    self.stats['service_stats'][service] = {'completed': 0, 'cancelled': 0, 'refunded': 0, 'blocked': 0}
                self.stats['service_stats'][service]['completed'] += 1
                
                # Calculate and store activation time
                activation_time = time.time() - activation['timestamp']
                self.stats['activation_times'].append(activation_time)
                
                # Clean up
                self.active_numbers.pop(phone, None)
                if port and port in self.modems:
                    self.modems[port]['status'] = 'active'
                    self.modems[port].pop('activation_id', None)
                
            elif status == 8:  # Cancelled by user
                logger.info(f"Activation cancelled: {phone} - {activation['service']}")
                self.stats['cancelled_activations'] += 1
                
                # Update service stats
                service = activation['service']
                if service not in self.stats['service_stats']:
                    self.stats['service_stats'][service] = {'completed': 0, 'cancelled': 0, 'refunded': 0, 'blocked': 0}
                self.stats['service_stats'][service]['cancelled'] += 1
                
                # Clean up
                self.active_numbers.pop(phone, None)
                if port and port in self.modems:
                    self.modems[port]['status'] = 'active'
                    self.modems[port].pop('activation_id', None)
                
            elif status == 10:  # Refunded
                logger.info(f"Activation refunded: {phone} - {activation['service']}")
                self.stats['refunded_activations'] += 1
                
                # Update service stats
                service = activation['service']
                if service not in self.stats['service_stats']:
                    self.stats['service_stats'][service] = {'completed': 0, 'cancelled': 0, 'refunded': 0, 'blocked': 0}
                self.stats['service_stats'][service]['refunded'] += 1
                
                # Clean up
                self.active_numbers.pop(phone, None)
                if port and port in self.modems:
                    self.modems[port]['status'] = 'active'
                    self.modems[port].pop('activation_id', None)
            
            else:
                logger.warning(f"Received unknown status code: {status} for activation {activation_id}")
            
            return jsonify({
                'status': 'SUCCESS'
            })

        except Exception as e:
            logger.error(f"Error in finish_activation: {e}", exc_info=True)
            return jsonify({
                'status': 'ERROR',
                'error': 'Internal Server Error'
            })

    def save_activation(self, phone: str, service: str, status: str):
        """Save activation to history file."""
        try:
            if status == 'completed':  # Only save completed activations
                timestamp = time.time()
                # Get activation details from active_numbers
                activation_details = self.active_numbers.get(phone, {})
                activation_id = activation_details.get('activation_id', 'unknown')
                
                entry = {
                    'activation_id': activation_id,
                    'phone': phone,
                    'service': service,
                    'timestamp': timestamp,
                    'date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp)),
                    'status': status,
                    'sum': activation_details.get('sum', 0)
                }
                
                # Update in-memory record
                if phone not in self.completed_activations:
                    self.completed_activations[phone] = {}
                self.completed_activations[phone][service] = {
                    'timestamp': timestamp,
                    'activation_id': activation_id
                }
                
                # Append to file
                with open(self.activation_log_file, 'a') as f:
                    f.write(json.dumps(entry) + '\n')
                    
                logger.info(f"Saved activation: {phone} - {service} - Activation ID: {activation_id}")
        except Exception as e:
            logger.error(f"Error saving activation: {e}")

    def handle_push_sms(self, data):
        """Handle PUSH_SMS request."""
        try:
            sms_id = data.get('smsId')
            phone = data.get('phone')
            phone_from = data.get('phoneFrom')
            text = data.get('text')

            if not all([sms_id, phone, phone_from, text]):
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Missing required fields'
                })

            # Validate types
            if not isinstance(sms_id, (int, float)) or not isinstance(phone, (int, float)):
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Invalid field types'
                })
            if not isinstance(phone_from, str):
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Invalid field types'
                })
            if not isinstance(text, str):
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Invalid field types'
                })

            # Log the SMS
            logger.info(f"Received SMS - ID: {sms_id}, From: {phone_from}, To: {phone}, Text: {text}")

            # Find active activation for this phone number
            str_phone = str(phone)
            activation = self.active_numbers.get(str_phone)
            if not activation:
                logger.warning(f"Received SMS for unknown activation: {phone}")
                return jsonify({
                    'status': 'ERROR',
                    'error': 'No active activation found for this number'
                })

            # Forward SMS to SMSHUB immediately
            try:
                if self.smshub:  # Make sure smshub client is initialized
                    response = self.smshub.push_sms(
                        sms_id=sms_id,
                        phone=phone,  # Already validated as int
                        phone_from=phone_from,
                        text=text
                    )
                    if response:
                        logger.info(f"Successfully forwarded SMS to SMSHUB - ID: {sms_id}")
                        return jsonify({
                            'status': 'SUCCESS'
                        })
                    else:
                        logger.error(f"Failed to forward SMS to SMSHUB - ID: {sms_id}, Response: {response}")
                        return jsonify({
                            'status': 'ERROR',
                            'error': 'Failed to forward SMS'
                        })
                else:
                    logger.error("SMSHUB client not initialized")
                    return jsonify({
                        'status': 'ERROR',
                        'error': 'SMSHUB client not initialized'
                    })
            except Exception as e:
                logger.error(f"Error forwarding SMS to SMSHUB: {e}", exc_info=True)
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Internal Server Error'
                })
        except Exception as e:
            logger.error(f"Error in push_sms: {e}", exc_info=True)
            return jsonify({
                'status': 'ERROR',
                'error': 'Internal Server Error'
            })

    def register_modem(self, key: str, modem_info: dict):
        """Register a modem with the server."""
        try:
            logger.info(f"Registering modem with key: {key}")
            logger.info(f"Modem info: {modem_info}")
            
            # Set required fields
            modem_info['operator'] = 'physic'
            modem_info['country'] = 'usaphysical'
            
            # Use port as key instead of phone number
            port = modem_info.get('port')
            if not port:
                logger.error("No port specified in modem_info")
                raise ValueError("No port specified in modem_info")
            
            # Store modem info using port as key
            self.modems[port] = modem_info
            logger.info(f"Successfully registered modem: {port} with status: {modem_info.get('status', 'unknown')}")
            logger.info(f"Current modems: {self.modems}")
            
        except Exception as e:
            logger.error(f"Error registering modem: {e}")
            logger.error("Full error details:", exc_info=True)
            raise

    def unregister_modem(self, phone_number: str) -> None:
        """Unregister a modem."""
        if phone_number in self.modems:
            logger.info(f"Unregistering modem: {phone_number}")
            self.modems.pop(phone_number)

    def get_service_quantities(self):
        """Get current quantities for all services."""
        try:
            # Get active modems count
            active_modems = len([m for m in self.modems.values() 
                               if m.get('status') == 'active'])
            
            # Get service quantities considering blocks and activation limits
            quantities = {}
            for service, enabled in config.get('services', {}).items():
                if not enabled:
                    continue
                    
                # Count available phones for this service
                available_count = 0
                for modem in self.modems.values():
                    if modem.get('status') != 'active':
                        continue
                    phone = modem.get('phone')
                    if not phone:
                        continue
                    # Check if service is available for this phone
                    if self.activation_history.is_service_available(phone, service):
                        available_count += 1
                
                quantities[service] = {
                    'quantity': available_count,
                    'active': len([1 for num in self.active_numbers.values() 
                                 if num.get('service') == service]),
                    'completed': len([1 for nums in self.completed_activations.values() 
                                    if service in nums])
                }
            
            return quantities
            
        except Exception as e:
            logger.error(f"Error getting service quantities: {e}")
            return {}

    def run(self):
        """Run the server."""
        self.app.run(
            host=self.host, 
            port=self.port, 
            debug=False, 
            threaded=True
        )

    def stop(self):
        """Stop the server and cleanup."""
        pass

    def handle_incoming_sms(self, phone: str, sender: str, text: str):
        """Handle incoming SMS message."""
        try:
            # Log the incoming SMS first
            logger.info("========== HANDLING INCOMING SMS ==========")
            logger.info(f"Original Phone: {phone}")
            logger.info(f"Sender: {sender}")
            logger.info(f"Text: {text}")
            
            # First, strip any '+' symbols and spaces - they're just formatting
            clean_phone = phone.lstrip('+').replace(' ', '').replace('-', '')
            
            # Now apply US phone number rules:
            # - If 10 digits, add '1' prefix
            # - If 11 digits, must start with '1'
            # - Any other length is invalid
            if len(clean_phone) == 10:
                normalized_phone = '1' + clean_phone
            elif len(clean_phone) == 11 and clean_phone.startswith('1'):
                normalized_phone = clean_phone
            else:
                logger.error(f"Invalid phone number format: {phone} (cleaned: {clean_phone})")
                return False
                
            logger.info(f"Normalized phone: {normalized_phone}")
            
            # Look up activation with normalized number
            activation = self.active_numbers.get(normalized_phone)
            if not activation:
                logger.warning(f"Received SMS for unknown activation: {normalized_phone}")
                logger.warning(f"Available active numbers: {list(self.active_numbers.keys())}")
                return False
            
            # Generate SMS ID
            sms_id = int(time.time() * 1000)  # Use timestamp as SMS ID
            logger.info(f"Generated SMS ID: {sms_id}")
            
            # Forward to SMS Hub with retries
            max_retries = 3  # Reduced from 10 for testing
            retry_delay = 1  # Reduced from 10 for testing
            
            for attempt in range(max_retries):
                try:
                    if not self.smshub:
                        logger.error("SMS Hub client not initialized")
                        return False
                        
                    # Clean phone number and sender for API request
                    clean_phone = normalized_phone.lstrip('+').replace('-', '').replace(' ', '')
                    clean_sender = sender.lstrip('+').replace('-', '').replace(' ', '')
                    
                    # Get the exact service code that was stored from GET_NUMBER
                    service_code = activation.get('service')  # Use directly, no mapping needed
                    logger.info(f"Using original service code from GET_NUMBER request: '{service_code}'")
                    
                    # Log the request details
                    logger.info(f"Sending SMS to SMS Hub - Attempt {attempt + 1}")
                    request_data = {
                        "smsId": sms_id,
                        "phoneFrom": service_code,  # Use exact service code from activation
                        "phone": int(clean_phone),
                        "text": text,
                        "action": "PUSH_SMS",
                        "key": "15431U1ea5e5b53572512438b03fbe8f96fa10"
                    }
                    logger.info(f"Request details: {json.dumps(request_data, indent=2)}")
                    
                    # Make the API call
                    response = self.smshub_integration.push_sms(
                        sms_id=sms_id,
                        phone=int(clean_phone),  # Must be numeric
                        phone_from=service_code,  # Pass service code instead of sender
                        text=text
                    )
                    
                    # Log the raw response for debugging
                    logger.info(f"SMS Hub raw response: {response}")
                    
                    if response and response.get('status') == 'SUCCESS':
                        logger.info(f"Successfully forwarded SMS to SMS Hub - ID: {sms_id}")
                        # Log successful delivery
                        self.log_sms_delivery(
                            activation_id=activation['activation_id'],
                            sms_text=text,
                            delivery_status='delivered'
                        )
                        return True
                    else:
                        logger.error(f"SMS Hub returned unexpected response format: {response}")
                    
                    logger.warning(f"Failed to forward SMS - Attempt {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:  # Don't sleep on last attempt
                        time.sleep(retry_delay)
                    
                except Exception as e:
                    logger.error(f"Error forwarding SMS (attempt {attempt + 1}): {str(e)}")
                    logger.error("Full error details:", exc_info=True)
                    if attempt < max_retries - 1:  # Don't sleep on last attempt
                        time.sleep(retry_delay)
                    
            # Log failed delivery after all retries
            self.log_sms_delivery(
                activation_id=activation['activation_id'],
                sms_text=text,
                delivery_status='failed'
            )
            
            logger.error(f"Failed to forward SMS after {max_retries} attempts")
            return False
            
        except Exception as e:
            logger.error(f"Error handling incoming SMS: {e}")
            logger.error("Full error details:", exc_info=True)
            return False

    def handle_get_services(self, data):
        """Handle GET_SERVICES request."""
        try:
            # Validate API key
            key = data.get('key')
            if not key:
                return jsonify({
                    'status': 'ERROR',
                    'error': 'Missing API key'
                })

            # Count active modems
            active_modems = len([m for m in self.modems.values() 
                               if m.get('status') == 'active'])

            logger.info(f"GET_SERVICES request - Active modems: {active_modems}")
            logger.debug(f"Current modems: {self.modems}")

            # Get enabled services from config
            enabled_services = config.get('services', {})

            # Create operator map with services
            operator_map = {}
            for service_id, enabled in enabled_services.items():
                if enabled:  # Only include enabled services
                    operator_map[service_id] = active_modems

            # Always return a response with the country list
            country_list = [{
                'country': 'usaphysical',
                'operatorMap': {
                    'physic': operator_map
                }
            }]

            response = {
                'status': 'SUCCESS',
                'countryList': country_list
            }

            logger.info(f"GET_SERVICES response: {response}")
            return jsonify(response)

        except Exception as e:
            logger.error(f"Error in get_services: {e}", exc_info=True)
            return jsonify({
                'status': 'ERROR',
                'error': 'Internal Server Error'
            })

    def log_activation_status(self, activation_id: int, status: int, phone: str = None):
        """Log activation status change."""
        try:
            if self.activation_logger:
                self.activation_logger.log_activation_status_update(
                    activation_id=activation_id,
                    status=status,
                    additional_data={'phone': phone}
                )
            else:
                # Fallback to basic logging
                logger.info(f"Activation status change - ID: {activation_id}, Status: {status}, Phone: {phone}")
        except Exception as e:
            logger.error(f"Error logging activation status: {e}")

    def log_sms_delivery(self, activation_id: int, sms_text: str, delivery_status: str):
        """Log SMS delivery status."""
        try:
            if self.activation_logger:
                self.activation_logger.log_sms_delivered(
                    activation_id=activation_id,
                    text=sms_text,
                    recipient='SMS Hub',
                    delivery_status=delivery_status
                )
            else:
                # Fallback to basic logging
                logger.info(f"SMS delivery - ID: {activation_id}, Status: {delivery_status}, Text: {sms_text}")
        except Exception as e:
            logger.error(f"Error logging SMS delivery: {e}")
            logger.error("Full error details:", exc_info=True)