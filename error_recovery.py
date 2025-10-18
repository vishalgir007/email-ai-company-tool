# error_recovery.py
# Comprehensive error recovery and monitoring system
# Implements exponential backoff, circuit breaker, logging, and monitoring

import time
import logging
import asyncio
import threading
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import json
import os
from functools import wraps

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('email_tool.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Circuit is open, failing fast
    HALF_OPEN = "half_open"  # Testing if service has recovered

@dataclass
class RetryConfig:
    """Configuration for exponential backoff retry logic"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_multiplier: float = 2.0
    jitter: bool = True

@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker pattern"""
    failure_threshold: int = 5  # Number of failures before opening circuit
    recovery_timeout: int = 60  # Seconds to wait before trying half-open
    success_threshold: int = 2  # Successes needed to close circuit from half-open

class MetricsCollector:
    """Collects and tracks metrics for monitoring"""
    
    def __init__(self):
        self.metrics = {
            'requests_total': 0,
            'requests_success': 0,
            'requests_failed': 0,
            'requests_retried': 0,
            'circuit_breaker_trips': 0,
            'average_response_time': 0.0,
            'response_times': [],
            'errors_by_type': {},
            'domains_processed': set(),
            'cache_hits': 0,
            'cache_misses': 0
        }
        self.start_time = datetime.now()
        self._lock = threading.Lock()
    
    def record_request(self, domain: str, success: bool, response_time: float, error_type: str = None):
        """Record a request with its outcome"""
        with self._lock:
            self.metrics['requests_total'] += 1
            self.metrics['domains_processed'].add(domain)
            self.metrics['response_times'].append(response_time)
            
            if success:
                self.metrics['requests_success'] += 1
            else:
                self.metrics['requests_failed'] += 1
                if error_type:
                    self.metrics['errors_by_type'][error_type] = self.metrics['errors_by_type'].get(error_type, 0) + 1
            
            # Update average response time
            if self.metrics['response_times']:
                self.metrics['average_response_time'] = sum(self.metrics['response_times']) / len(self.metrics['response_times'])
    
    def record_retry(self):
        """Record a retry attempt"""
        with self._lock:
            self.metrics['requests_retried'] += 1
    
    def record_circuit_trip(self):
        """Record a circuit breaker trip"""
        with self._lock:
            self.metrics['circuit_breaker_trips'] += 1
    
    def record_cache_hit(self):
        """Record a cache hit"""
        with self._lock:
            self.metrics['cache_hits'] += 1
    
    def record_cache_miss(self):
        """Record a cache miss"""
        with self._lock:
            self.metrics['cache_misses'] += 1
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics"""
        with self._lock:
            runtime = datetime.now() - self.start_time
            cache_total = self.metrics['cache_hits'] + self.metrics['cache_misses']
            cache_hit_rate = (self.metrics['cache_hits'] / cache_total * 100) if cache_total > 0 else 0
            
            return {
                'runtime_seconds': runtime.total_seconds(),
                'requests_total': self.metrics['requests_total'],
                'success_rate': (self.metrics['requests_success'] / self.metrics['requests_total'] * 100) if self.metrics['requests_total'] > 0 else 0,
                'failure_rate': (self.metrics['requests_failed'] / self.metrics['requests_total'] * 100) if self.metrics['requests_total'] > 0 else 0,
                'retry_rate': (self.metrics['requests_retried'] / self.metrics['requests_total'] * 100) if self.metrics['requests_total'] > 0 else 0,
                'average_response_time': self.metrics['average_response_time'],
                'circuit_breaker_trips': self.metrics['circuit_breaker_trips'],
                'unique_domains': len(self.metrics['domains_processed']),
                'cache_hit_rate': cache_hit_rate,
                'errors_by_type': self.metrics['errors_by_type'],
                'requests_per_second': self.metrics['requests_total'] / runtime.total_seconds() if runtime.total_seconds() > 0 else 0
            }
    
    def save_report(self, filepath: str = "metrics_report.json"):
        """Save metrics report to file"""
        summary = self.get_summary()
        # Convert set to list for JSON serialization
        summary['unique_domains_list'] = list(self.metrics['domains_processed'])
        del summary  # Remove non-serializable set
        
        with open(filepath, 'w') as f:
            json.dump(self.get_summary(), f, indent=2, default=str)
        
        logger.info(f"Metrics report saved to {filepath}")

class CircuitBreaker:
    """Circuit breaker implementation for handling cascading failures"""
    
    def __init__(self, config: CircuitBreakerConfig, name: str = "default"):
        self.config = config
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self._lock = threading.Lock()
        
        logger.info(f"Circuit breaker '{name}' initialized: {config}")
    
    def can_execute(self) -> bool:
        """Check if the circuit allows execution"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            elif self.state == CircuitState.OPEN:
                if self.last_failure_time and \
                   time.time() - self.last_failure_time >= self.config.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(f"Circuit breaker '{self.name}' transitioning to HALF_OPEN")
                    return True
                return False
            elif self.state == CircuitState.HALF_OPEN:
                return True
            return False
    
    def record_success(self):
        """Record a successful operation"""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info(f"Circuit breaker '{self.name}' CLOSED after recovery")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = max(0, self.failure_count - 1)  # Gradually reduce failure count
    
    def record_failure(self):
        """Record a failed operation"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.CLOSED and \
               self.failure_count >= self.config.failure_threshold:
                self.state = CircuitState.OPEN
                metrics_collector.record_circuit_trip()
                logger.warning(f"Circuit breaker '{self.name}' OPENED after {self.failure_count} failures")
            elif self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker '{self.name}' reopened during testing")

class RetryHandler:
    """Handles exponential backoff retry logic"""
    
    def __init__(self, config: RetryConfig):
        self.config = config
    
    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt with exponential backoff"""
        delay = min(
            self.config.base_delay * (self.config.backoff_multiplier ** attempt),
            self.config.max_delay
        )
        
        if self.config.jitter:
            import random
            delay *= (0.5 + random.random() * 0.5)  # Add jitter: 50-100% of calculated delay
        
        return delay
    
    async def retry_async(self, func: Callable, *args, **kwargs):
        """Retry an async function with exponential backoff"""
        last_exception = None
        
        for attempt in range(self.config.max_attempts):
            try:
                result = await func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Operation succeeded after {attempt + 1} attempts")
                return result
            except Exception as e:
                last_exception = e
                if attempt == self.config.max_attempts - 1:
                    logger.error(f"Operation failed after {self.config.max_attempts} attempts: {e}")
                    break
                
                delay = self.calculate_delay(attempt)
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s")
                metrics_collector.record_retry()
                await asyncio.sleep(delay)
        
        raise last_exception
    
    def retry_sync(self, func: Callable, *args, **kwargs):
        """Retry a sync function with exponential backoff"""
        last_exception = None
        
        for attempt in range(self.config.max_attempts):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"Operation succeeded after {attempt + 1} attempts")
                return result
            except Exception as e:
                last_exception = e
                if attempt == self.config.max_attempts - 1:
                    logger.error(f"Operation failed after {self.config.max_attempts} attempts: {e}")
                    break
                
                delay = self.calculate_delay(attempt)
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s")
                metrics_collector.record_retry()
                time.sleep(delay)
        
        raise last_exception

# Global instances
metrics_collector = MetricsCollector()
default_retry_handler = RetryHandler(RetryConfig())
default_circuit_breaker = CircuitBreaker(CircuitBreakerConfig(), "default")

# Domain-specific circuit breakers
domain_circuit_breakers: Dict[str, CircuitBreaker] = {}

def get_domain_circuit_breaker(domain: str) -> CircuitBreaker:
    """Get or create a circuit breaker for a specific domain"""
    if domain not in domain_circuit_breakers:
        domain_circuit_breakers[domain] = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30),
            f"domain_{domain}"
        )
    return domain_circuit_breakers[domain]

def with_error_recovery(
    retry_config: Optional[RetryConfig] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    track_metrics: bool = True
):
    """Decorator for adding error recovery to functions"""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            cb = circuit_breaker or default_circuit_breaker
            retry_handler = RetryHandler(retry_config or RetryConfig())
            
            if not cb.can_execute():
                error = f"Circuit breaker '{cb.name}' is OPEN"
                logger.warning(error)
                raise Exception(error)
            
            start_time = time.time()
            domain = kwargs.get('domain', 'unknown')
            
            try:
                result = await retry_handler.retry_async(func, *args, **kwargs)
                cb.record_success()
                
                if track_metrics:
                    response_time = time.time() - start_time
                    metrics_collector.record_request(domain, True, response_time)
                
                return result
            except Exception as e:
                cb.record_failure()
                
                if track_metrics:
                    response_time = time.time() - start_time
                    metrics_collector.record_request(domain, False, response_time, type(e).__name__)
                
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            cb = circuit_breaker or default_circuit_breaker
            retry_handler = RetryHandler(retry_config or RetryConfig())
            
            if not cb.can_execute():
                error = f"Circuit breaker '{cb.name}' is OPEN"
                logger.warning(error)
                raise Exception(error)
            
            start_time = time.time()
            domain = kwargs.get('domain', 'unknown')
            
            try:
                result = retry_handler.retry_sync(func, *args, **kwargs)
                cb.record_success()
                
                if track_metrics:
                    response_time = time.time() - start_time
                    metrics_collector.record_request(domain, True, response_time)
                
                return result
            except Exception as e:
                cb.record_failure()
                
                if track_metrics:
                    response_time = time.time() - start_time
                    metrics_collector.record_request(domain, False, response_time, type(e).__name__)
                
                raise
        
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
    return decorator

class HealthChecker:
    """Health check system for monitoring service availability"""
    
    def __init__(self):
        self.checks = {}
        self.last_check_times = {}
    
    def register_check(self, name: str, check_func: Callable, interval: int = 60):
        """Register a health check function"""
        self.checks[name] = {
            'func': check_func,
            'interval': interval,
            'status': 'unknown',
            'last_error': None
        }
        logger.info(f"Registered health check: {name}")
    
    async def run_check(self, name: str) -> bool:
        """Run a specific health check"""
        if name not in self.checks:
            logger.error(f"Health check '{name}' not found")
            return False
        
        check = self.checks[name]
        try:
            if asyncio.iscoroutinefunction(check['func']):
                result = await check['func']()
            else:
                result = check['func']()
            
            check['status'] = 'healthy' if result else 'unhealthy'
            check['last_error'] = None
            self.last_check_times[name] = time.time()
            return result
        except Exception as e:
            check['status'] = 'error'
            check['last_error'] = str(e)
            self.last_check_times[name] = time.time()
            logger.error(f"Health check '{name}' failed: {e}")
            return False
    
    async def run_all_checks(self) -> Dict[str, Any]:
        """Run all registered health checks"""
        results = {}
        for name in self.checks:
            results[name] = {
                'healthy': await self.run_check(name),
                'status': self.checks[name]['status'],
                'last_error': self.checks[name]['last_error'],
                'last_check_time': self.last_check_times.get(name)
            }
        return results
    
    def get_status_summary(self) -> Dict[str, Any]:
        """Get overall health status summary"""
        healthy_checks = sum(1 for check in self.checks.values() if check['status'] == 'healthy')
        total_checks = len(self.checks)
        
        return {
            'overall_healthy': healthy_checks == total_checks,
            'healthy_checks': healthy_checks,
            'total_checks': total_checks,
            'checks': {name: check['status'] for name, check in self.checks.items()}
        }

# Global health checker
health_checker = HealthChecker()

def setup_monitoring():
    """Set up comprehensive monitoring and logging"""
    logger.info("Setting up comprehensive monitoring system")
    
    # Register basic health checks
    def check_disk_space():
        import shutil
        free_bytes = shutil.disk_usage('.').free
        free_gb = free_bytes / (1024**3)
        return free_gb > 1.0  # At least 1GB free
    
    def check_cache_db():
        try:
            import sqlite3
            conn = sqlite3.connect('Dataset/search_cache.db')
            conn.execute('SELECT 1')
            conn.close()
            return True
        except:
            return False
    
    health_checker.register_check('disk_space', check_disk_space)
    health_checker.register_check('cache_database', check_cache_db)
    
    logger.info("Monitoring system setup complete")

def print_metrics_summary():
    """Print a formatted metrics summary"""
    summary = metrics_collector.get_summary()
    
    print("\n" + "="*60)
    print("PERFORMANCE METRICS SUMMARY")
    print("="*60)
    print(f"Runtime: {summary['runtime_seconds']:.1f}s")
    print(f"Total Requests: {summary['requests_total']}")
    print(f"Success Rate: {summary['success_rate']:.1f}%")
    print(f"Failure Rate: {summary['failure_rate']:.1f}%")
    print(f"Retry Rate: {summary['retry_rate']:.1f}%")
    print(f"Avg Response Time: {summary['average_response_time']:.3f}s")
    print(f"Requests/Second: {summary['requests_per_second']:.2f}")
    print(f"Circuit Breaker Trips: {summary['circuit_breaker_trips']}")
    print(f"Unique Domains: {summary['unique_domains']}")
    print(f"Cache Hit Rate: {summary['cache_hit_rate']:.1f}%")
    
    if summary['errors_by_type']:
        print("\nError Breakdown:")
        for error_type, count in summary['errors_by_type'].items():
            print(f"  {error_type}: {count}")
    
    print("="*60)

# Initialize monitoring on import
setup_monitoring()