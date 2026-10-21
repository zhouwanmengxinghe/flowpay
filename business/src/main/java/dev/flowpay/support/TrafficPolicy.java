package dev.flowpay.support;

import java.time.Instant;
import java.util.List;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/** Redis 只负责快速窗口计数；每日受理数量来自持久化的 runs，重启不会重置配额。 */
@Service
public class TrafficPolicy {
    private final StringRedisTemplate redis;
    private final JdbcTemplate db;
    private static final DefaultRedisScript<Long> WINDOW = new DefaultRedisScript<>("""
        local current=tonumber(redis.call('GET',KEYS[1]) or '0')
        if current>=tonumber(ARGV[1]) then return 0 end
        redis.call('INCR',KEYS[1]); redis.call('EXPIRE',KEYS[1],tonumber(ARGV[2])); return 1
        """, Long.class);
    public TrafficPolicy(StringRedisTemplate redis, JdbcTemplate db) { this.redis=redis; this.db=db; }
    public void window(String scope, int maximum, int seconds) {
        long slot=Instant.now().getEpochSecond()/seconds;
        try {
            Long allowed=redis.execute(WINDOW,List.of("flowpay:limit:"+scope+":"+slot),String.valueOf(maximum),String.valueOf(seconds+1));
            if(!Long.valueOf(1).equals(allowed)) throw new ApiError(429,"请求过于频繁，请稍后重试");
        } catch(ApiError e) { throw e; }
        catch(Exception e) { throw new ApiError(503,"流量保护暂不可用，请稍后重试"); }
    }
    /** 调用方已锁定商户行，并在同一事务中创建 run，配额不会因重复提交而重复扣减。 */
    public void admit(String tenant) {
        db.update("INSERT INTO tenant_limits(tenant_id) VALUES (?) ON CONFLICT DO NOTHING",tenant);
        var limit=db.queryForMap("SELECT * FROM tenant_limits WHERE tenant_id=?",tenant);
        Long today=db.queryForObject("SELECT count(*) FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.tenant_id=? AND r.created_at >= date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",Long.class,tenant);
        if(today>=((Number)limit.get("daily_runs")).longValue()) throw new ApiError(429,"本商户今日调查配额已用尽（UTC 日界）");
        Long active=db.queryForObject("SELECT count(*) FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.tenant_id=? AND r.status IN ('queued','running')",Long.class,tenant);
        if(active>=((Number)limit.get("active_runs")).longValue()) throw new ApiError(429,"本商户活动调查数量已达上限");
        Long queued=db.queryForObject("SELECT count(*) FROM runs WHERE status='queued'",Long.class);
        if(queued>=16) throw new ApiError(503,"系统等待队列已满，请稍后重试");
        window("tenant:"+tenant,((Number)limit.get("requests_per_minute")).intValue(),60);
    }
}
