# Reaper Optimization Configuration Guide

## Overview

This branch introduces an optional immediate database cleanup optimization for the Rucio reaper daemon while maintaining backwards compatibility with the traditional behavior.

## Configuration Modes

### Traditional Mode (Default)
**Configuration:** `enable_immediate_cleanup = false` (default, can be omitted)

```ini
[reaper]
# Traditional mode - no additional configuration needed
# enable_immediate_cleanup = false  # Default value, can be omitted
delay_seconds = 600                  # Standard replica selection delay
chunk_size = 100                     # Number of replicas to process per batch
```

**Behavior:**
- Database cleanup happens once after all physical deletions complete
- All successfully deleted files are returned to the main loop for cleanup
- Maintains the original, predictable behavior
- Easier to debug and understand
- More compatible with existing monitoring and tooling

### Immediate Cleanup Mode (Opt-in)
**Configuration:** `enable_immediate_cleanup = true`

```ini
[reaper]
enable_immediate_cleanup = true      # Enable immediate cleanup optimization
db_batch_size = 50                   # Batch size for immediate database cleanup (default: 50)
refresh_trigger_ratio = 80           # Percentage of delay_seconds before refreshing (default: 80)
delay_seconds = 600                  # Standard replica selection delay
chunk_size = 100                     # Number of replicas to process per batch
```

**Behavior:**
- Database cleanup happens in configurable batches during physical deletion
- Reduces database load and prevents race conditions between workers
- Better performance for high-throughput scenarios
- Provides detailed optimization metrics and logging

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enable_immediate_cleanup` | `false` | Enable/disable immediate database cleanup optimization |
| `db_batch_size` | `50` | Number of replicas to clean from database in each immediate batch |
| `refresh_trigger_ratio` | `80` | Percentage of `delay_seconds` after which to refresh remaining replicas (applies to both traditional and immediate cleanup modes) |
| `delay_seconds` | `600` | Standard delay for replica selection (existing parameter) |
| `chunk_size` | `100` | Number of replicas to process per iteration (existing parameter) |

## Replica Refresh Control

### Background
The reaper uses a `delay_seconds` mechanism to prevent multiple workers from processing the same replicas. When replicas are marked as `BEING_DELETED`, other workers will not select them until `delay_seconds` have passed since their last update.

### Refresh Mechanism
To prevent race conditions when processing takes longer than expected, the reaper can refresh the `updated_at` timestamp of remaining replicas:

```ini
[reaper]
delay_seconds = 600                  # Replicas become selectable by other workers after 10 minutes
refresh_trigger_ratio = 80           # Refresh remaining replicas after 80% of delay_seconds (8 minutes)
```

**How it works:**
1. Worker starts processing 100 replicas at time T=0
2. At T=8 minutes (80% of 10 minutes), if replicas are still being processed:
   - Worker calls `refresh_replicas()` on remaining unprocessed replicas
   - This updates their `updated_at` timestamp to current time
   - Other workers will wait another 10 minutes before selecting these replicas
3. Original worker continues processing without interference

### Refresh Configuration Examples

**Conservative (longer processing time allowed):**
```ini
[reaper]
delay_seconds = 900                  # 15 minutes before other workers can take over
refresh_trigger_ratio = 90           # Refresh after 13.5 minutes
```

**Aggressive (faster worker coordination):**
```ini
[reaper]
delay_seconds = 300                  # 5 minutes before other workers can take over  
refresh_trigger_ratio = 70           # Refresh after 3.5 minutes
```

**Multi-worker environment (balanced):**
```ini
[reaper]
delay_seconds = 600                  # Standard 10 minutes
refresh_trigger_ratio = 75           # Refresh after 7.5 minutes (leaves 2.5min buffer)
```

## Performance Tuning Examples

### High-Throughput Environment
Optimize for maximum performance with frequent immediate cleanups:

```ini
[reaper]
enable_immediate_cleanup = true
db_batch_size = 25                   # Smaller batches for more frequent cleanup
refresh_trigger_ratio = 70           # Refresh remaining replicas earlier
delay_seconds = 300                  # Shorter delay for faster processing
chunk_size = 200                     # Larger chunks for higher throughput
```

### Conservative Environment
Optimize for reliability with larger batches:

```ini
[reaper]
enable_immediate_cleanup = true
db_batch_size = 100                  # Larger batches, fewer database calls
refresh_trigger_ratio = 90           # Wait longer before refreshing
delay_seconds = 900                  # Longer delay for stability
chunk_size = 50                      # Smaller chunks for reliability
```

### Multi-Worker Environment
Optimize for coordination between multiple reaper workers:

```ini
[reaper]
enable_immediate_cleanup = true
db_batch_size = 30                   # Moderate batch size
refresh_trigger_ratio = 75           # Refresh before other workers can interfere
delay_seconds = 600                  # Standard delay
chunk_size = 100                     # Standard chunk size
```

## Monitoring and Debugging

### Log Messages

**Traditional Mode:**
```
DEBUG: Deletion complete for RSE CERN-PROD - processed 150 replicas, all 150 will be cleaned up by main loop (traditional mode)
DEBUG: Main loop cleanup SUCCESS - deleted 150 remaining replicas in 2.34 seconds
```

**Immediate Cleanup Mode:**
```
DEBUG: Starting deletion for RSE CERN-PROD with 150 replicas, enable_immediate_cleanup=True, db_batch_size=50, delay_seconds=600
DEBUG: Immediate cleanup SUCCESS: deleted 50 replicas from database (batch #1)
DEBUG: Immediate cleanup SUCCESS: deleted 50 replicas from database (batch #2)
DEBUG: Final cleanup SUCCESS: deleted 50 remaining replicas from database
DEBUG: Deletion complete for RSE CERN-PROD - processed 150 replicas, performed 3 immediate cleanups, total immediate cleaned: 150, remaining for main loop: 0
```

**Replica Refresh Messages:**
```
DEBUG: Refresh trigger time set to 480.0 seconds (80% of delay_seconds=600)
DEBUG: Refresh triggered after 485.2 seconds - refreshing 45 remaining replicas (out of 100 total)
DEBUG: Successfully refreshed 45 remaining replicas after 485.2 seconds
WARNING: Failed to bump updated_at for remaining replicas BEING_DELETED
```

### Configuration Verification

Check active configuration at startup:
```
DEBUG: Optimization configuration - enable_immediate_cleanup=True, db_batch_size=50, refresh_trigger_ratio=80%, delay_seconds=600, chunk_size=100, total_workers=4
```

### Replica Refresh Function

The `refresh_replicas()` function in `rucio.core.replica` provides the underlying mechanism:

```python
from rucio.core.replica import refresh_replicas

# Update the updated_at timestamp for replicas to prevent other workers from taking them
success = refresh_replicas(
    rse_id='CERN-PROD_DATADISK', 
    replicas=[
        {'scope': 'cms', 'name': 'file1.root'},
        {'scope': 'cms', 'name': 'file2.root'}
    ]
)
```

**Function behavior:**
- Updates `updated_at` timestamp to current time for replicas in `BEING_DELETED` state
- Uses temporary tables and Oracle hints for efficient bulk updates
- Returns `True` on success, `False` on database errors
- Only processes replicas that are actually in `BEING_DELETED` state
- Skips operation if no replicas need refreshing

## Migration Guide

### From Previous Versions
1. **No action required** - Traditional mode is the default
2. **To enable optimization** - Add `enable_immediate_cleanup = true` to `[reaper]` section
3. **To tune performance** - Adjust `db_batch_size` and `refresh_trigger_ratio` as needed

### Testing the Configuration
1. Start with traditional mode (default) to ensure compatibility
2. Enable immediate cleanup in a test environment
3. Monitor logs for optimization metrics
4. Tune batch sizes based on database performance and worker count
5. Deploy to production with validated settings

## New Core Functions

### `refresh_replicas(rse_id, replicas)`
Updates the `updated_at` timestamp of replicas in `BEING_DELETED` state to prevent race conditions.

**Parameters:**
- `rse_id`: RSE ID containing the replicas to refresh
- `replicas`: List of dictionaries with `scope` and `name` keys

**Returns:** `True` if successful, `False` on database errors

**Usage in reaper optimization:**
- Called automatically when `elapsed_time > trigger_time` 
- Only refreshes replicas that haven't been processed yet
- Prevents other workers from taking over long-running deletion jobs

### `get_replica_updated_at(replica)`
Utility function to get the `updated_at` timestamp for a specific replica (primarily for testing).

**Parameters:**
- `replica`: Dictionary with `scope`, `name`, and `rse_id` keys

**Returns:** `datetime` object with the replica's last update time

## Benefits

### Traditional Mode
- ✅ Backwards compatible
- ✅ Predictable behavior
- ✅ Easier debugging
- ✅ Works with existing monitoring
- ✅ No configuration changes needed

### Immediate Cleanup Mode
- ✅ Reduced database load
- ✅ Better performance in high-throughput scenarios
- ✅ Prevents race conditions between workers
- ✅ Configurable batch sizes
- ✅ Detailed performance metrics
- ✅ Better resource utilization
- ✅ Intelligent replica refresh timing
- ✅ Reduced worker conflicts in multi-worker environments

## Best Practices

1. **Start Conservative:** Begin with traditional mode, then enable optimizations if needed
2. **Monitor Performance:** Watch database load and reaper throughput when enabling immediate cleanup
3. **Tune Batch Sizes:** Adjust `db_batch_size` based on your database performance characteristics
4. **Consider Worker Count:** In multi-worker environments, smaller batch sizes may work better
5. **Test Thoroughly:** Validate the configuration in a test environment before production deployment
6. **Monitor Refresh Timing:** Watch for refresh trigger messages in logs to ensure proper timing
7. **Adjust Refresh Ratio:** If workers frequently conflict, reduce `refresh_trigger_ratio` to refresh earlier
8. **Balance Delays:** Shorter `delay_seconds` improves responsiveness but may increase worker conflicts

## Troubleshooting

### Common Issues

**Workers taking over each other's work:**
```ini
# Solution: Reduce refresh trigger ratio or increase delay
[reaper]
delay_seconds = 900              # Increase to 15 minutes
refresh_trigger_ratio = 70       # Refresh after 70% (10.5 minutes)
```

**Database performance issues with immediate cleanup:**
```ini
# Solution: Increase batch size to reduce DB calls
[reaper]
enable_immediate_cleanup = true
db_batch_size = 100             # Larger batches, fewer DB operations
```

**Slow processing causing timeouts:**
```ini
# Solution: Increase delay and refresh earlier
[reaper]
delay_seconds = 1200            # 20 minutes total
refresh_trigger_ratio = 60      # Refresh after 12 minutes
```
