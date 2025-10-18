# monitoring_config.py
# Monitoring and alerting configuration for the Email Company Tool

import os
import json
import time
from typing import Dict, Any, List
from dataclasses import dataclass
from error_recovery import RetryConfig, CircuitBreakerConfig

@dataclass
class MonitoringConfig:
    """Configuration for monitoring and alerting"""
    
    # Logging configuration
    log_level: str = "INFO"
    log_file: str = "email_tool.log"
    log_rotation_size: int = 10 * 1024 * 1024  # 10MB
    log_retention_days: int = 30
    
    # Metrics collection
    metrics_enabled: bool = True
    metrics_file: str = "metrics_report.json"
    metrics_retention_days: int = 7
    
    # Health checks
    health_check_interval: int = 60  # seconds
    health_check_timeout: int = 30   # seconds
    
    # Alerting thresholds
    error_rate_threshold: float = 10.0  # percentage
    response_time_threshold: float = 30.0  # seconds
    circuit_breaker_threshold: int = 3  # number of trips
    disk_space_threshold: float = 1.0  # GB minimum free space
    memory_usage_threshold: float = 80.0  # percentage
    
    # Performance monitoring
    slow_request_threshold: float = 10.0  # seconds
    cache_hit_rate_threshold: float = 50.0  # percentage minimum
    
    def __post_init__(self):
        """Initialize mutable defaults after dataclass creation"""
        if not hasattr(self, 'default_circuit_config'):
            # Circuit breaker configurations
            self.default_circuit_config = CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=60,
                success_threshold=2
            )
        
        if not hasattr(self, 'web_request_circuit_config'):    
            self.web_request_circuit_config = CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30,
                success_threshold=2
            )
        
        if not hasattr(self, 'default_retry_config'):    
            # Retry configurations
            self.default_retry_config = RetryConfig(
                max_attempts=3,
                base_delay=1.0,
                max_delay=60.0,
                backoff_multiplier=2.0,
                jitter=True
            )
        
        if not hasattr(self, 'web_request_retry_config'):    
            self.web_request_retry_config = RetryConfig(
                max_attempts=2,
                base_delay=1.0,
                max_delay=5.0,
                backoff_multiplier=2.0,
                jitter=True
            )
        
        if not hasattr(self, 'homepage_fetch_retry_config'):    
            self.homepage_fetch_retry_config = RetryConfig(
                max_attempts=2,
                base_delay=0.5,
                max_delay=3.0,
                backoff_multiplier=1.5,
                jitter=True
            )

def load_monitoring_config(config_file: str = "monitoring_config.json") -> MonitoringConfig:
    """Load monitoring configuration from file or return defaults"""
    
    if not os.path.exists(config_file):
        # Create default config file
        config = MonitoringConfig()
        save_monitoring_config(config, config_file)
        return config
    
    try:
        with open(config_file, 'r') as f:
            data = json.load(f)
        
        # Remove nested configs from data since they're set in __post_init__
        nested_configs = {}
        config_keys = [
            'default_circuit_config', 'web_request_circuit_config',
            'default_retry_config', 'web_request_retry_config', 
            'homepage_fetch_retry_config'
        ]
        
        for key in config_keys:
            if key in data:
                nested_configs[key] = data.pop(key)
        
        # Create MonitoringConfig with basic fields only
        config = MonitoringConfig(**data)
        
        # Now manually set the nested configs after initialization
        if 'default_circuit_config' in nested_configs:
            config.default_circuit_config = CircuitBreakerConfig(**nested_configs['default_circuit_config'])
        if 'web_request_circuit_config' in nested_configs:
            config.web_request_circuit_config = CircuitBreakerConfig(**nested_configs['web_request_circuit_config'])
        if 'default_retry_config' in nested_configs:
            config.default_retry_config = RetryConfig(**nested_configs['default_retry_config'])
        if 'web_request_retry_config' in nested_configs:
            config.web_request_retry_config = RetryConfig(**nested_configs['web_request_retry_config'])
        if 'homepage_fetch_retry_config' in nested_configs:
            config.homepage_fetch_retry_config = RetryConfig(**nested_configs['homepage_fetch_retry_config'])
        
        return config
        
    except Exception as e:
        print(f"Warning: Failed to load monitoring config from {config_file}: {e}")
        print("Using default configuration")
        return MonitoringConfig()

def save_monitoring_config(config: MonitoringConfig, config_file: str = "monitoring_config.json"):
    """Save monitoring configuration to file"""
    
    # Convert dataclasses to dict for JSON serialization
    def dataclass_to_dict(obj):
        if hasattr(obj, '__dataclass_fields__'):
            return {k: dataclass_to_dict(v) for k, v in obj.__dict__.items()}
        return obj
    
    try:
        config_dict = dataclass_to_dict(config)
        
        with open(config_file, 'w') as f:
            json.dump(config_dict, f, indent=2, default=str)
        
        print(f"Monitoring configuration saved to {config_file}")
        
    except Exception as e:
        print(f"Error saving monitoring config to {config_file}: {e}")

# Alert definitions
ALERT_TEMPLATES = {
    'high_error_rate': {
        'title': 'High Error Rate Detected',
        'message': 'Error rate is {error_rate:.1f}% (threshold: {threshold:.1f}%)',
        'severity': 'warning'
    },
    'slow_response_time': {
        'title': 'Slow Response Time',
        'message': 'Average response time is {response_time:.2f}s (threshold: {threshold:.2f}s)',
        'severity': 'warning'
    },
    'circuit_breaker_trip': {
        'title': 'Circuit Breaker Tripped',
        'message': 'Circuit breaker "{circuit_name}" has tripped {count} times',
        'severity': 'error'
    },
    'low_disk_space': {
        'title': 'Low Disk Space',
        'message': 'Available disk space: {free_space:.1f}GB (threshold: {threshold:.1f}GB)',
        'severity': 'critical'
    },
    'high_memory_usage': {
        'title': 'High Memory Usage',
        'message': 'Memory usage: {memory_usage:.1f}% (threshold: {threshold:.1f}%)',
        'severity': 'warning'
    },
    'low_cache_hit_rate': {
        'title': 'Low Cache Hit Rate',
        'message': 'Cache hit rate: {hit_rate:.1f}% (threshold: {threshold:.1f}%)',
        'severity': 'info'
    }
}

class AlertManager:
    """Manages alerts and notifications"""
    
    def __init__(self, config: MonitoringConfig):
        self.config = config
        self.active_alerts = {}
        self.alert_history = []
    
    def check_and_alert(self, metrics: Dict[str, Any]):
        """Check metrics against thresholds and trigger alerts"""
        
        alerts_triggered = []
        
        # Check error rate
        if metrics.get('failure_rate', 0) > self.config.error_rate_threshold:
            alert = self._create_alert(
                'high_error_rate',
                error_rate=metrics['failure_rate'],
                threshold=self.config.error_rate_threshold
            )
            alerts_triggered.append(alert)
        
        # Check response time
        if metrics.get('average_response_time', 0) > self.config.response_time_threshold:
            alert = self._create_alert(
                'slow_response_time',
                response_time=metrics['average_response_time'],
                threshold=self.config.response_time_threshold
            )
            alerts_triggered.append(alert)
        
        # Check circuit breaker trips
        if metrics.get('circuit_breaker_trips', 0) > self.config.circuit_breaker_threshold:
            alert = self._create_alert(
                'circuit_breaker_trip',
                circuit_name='default',
                count=metrics['circuit_breaker_trips']
            )
            alerts_triggered.append(alert)
        
        # Check cache hit rate
        if metrics.get('cache_hit_rate', 100) < self.config.cache_hit_rate_threshold:
            alert = self._create_alert(
                'low_cache_hit_rate',
                hit_rate=metrics['cache_hit_rate'],
                threshold=self.config.cache_hit_rate_threshold
            )
            alerts_triggered.append(alert)
        
        # Store and return alerts
        for alert in alerts_triggered:
            self.active_alerts[alert['id']] = alert
            self.alert_history.append(alert)
        
        return alerts_triggered
    
    def _create_alert(self, alert_type: str, **kwargs) -> Dict[str, Any]:
        """Create an alert with the given type and parameters"""
        
        template = ALERT_TEMPLATES.get(alert_type, {
            'title': 'Unknown Alert',
            'message': 'Alert triggered: {alert_type}',
            'severity': 'info'
        })
        
        alert_id = f"{alert_type}_{hash(str(kwargs)) % 10000}"
        
        return {
            'id': alert_id,
            'type': alert_type,
            'title': template['title'],
            'message': template['message'].format(**kwargs),
            'severity': template['severity'],
            'timestamp': time.time(),
            'parameters': kwargs
        }
    
    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """Get all active alerts"""
        return list(self.active_alerts.values())
    
    def clear_alert(self, alert_id: str):
        """Clear an active alert"""
        if alert_id in self.active_alerts:
            del self.active_alerts[alert_id]
    
    def clear_all_alerts(self):
        """Clear all active alerts"""
        self.active_alerts.clear()

# Global monitoring configuration
monitoring_config = load_monitoring_config()
alert_manager = AlertManager(monitoring_config)

if __name__ == '__main__':
    # Test configuration
    print("Monitoring Configuration:")
    print(f"Log Level: {monitoring_config.log_level}")
    print(f"Error Rate Threshold: {monitoring_config.error_rate_threshold}%")
    print(f"Response Time Threshold: {monitoring_config.response_time_threshold}s")
    print(f"Circuit Breaker Config: {monitoring_config.default_circuit_config}")
    print(f"Retry Config: {monitoring_config.default_retry_config}")