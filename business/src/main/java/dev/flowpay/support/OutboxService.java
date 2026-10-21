package dev.flowpay.support;

import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/** 与业务事务一起保存轻量通知；任务内容和状态仍只存在业务表。 */
@Component
public class OutboxService {
    private final JdbcTemplate db; private final Json json; private final boolean enabled;
    public OutboxService(JdbcTemplate db,Json json,@Value("${support.dispatch-notifications:false}") boolean enabled) {
        this.db=db; this.json=json; this.enabled=enabled;
    }
    public void queued(String run) {
        if(!enabled) return;
        db.update("INSERT INTO outbox_events(id,event_type,run_id,payload) VALUES (?,?,?,?::jsonb)",
            SupportService.id("outbox"),"run.queued",run,json.write(Map.of("run_id",run,"schema_version",1)));
    }
}
