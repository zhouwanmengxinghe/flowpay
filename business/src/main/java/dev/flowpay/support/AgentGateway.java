package dev.flowpay.support;

import jakarta.annotation.PreDestroy;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Future;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class AgentGateway {
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    private final ThreadPoolExecutor executor;
    private final String url; private final String key; private final Json json;
    private final Map<String,Future<?>> active=new ConcurrentHashMap<>();
    public AgentGateway(Json json, @Value("${support.agent-url}") String url, @Value("${support.agent-key}") String key,
                        @Value("${support.workers}") int workers, @Value("${support.queue-capacity}") int capacity) {
        if(key.isBlank()) throw new IllegalArgumentException("SUPPORT_AGENT_KEY is required");
        this.url=url; this.key=key; this.json=json;
        executor = new ThreadPoolExecutor(workers,workers,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<>(capacity));
    }
    public void submit(Runnable task) { executor.execute(task); }
    public synchronized boolean dispatch(String run,Runnable task) {
        if(active.containsKey(run)||active.size()>=executor.getMaximumPoolSize()) return false;
        var future=new java.util.concurrent.FutureTask<Void>(() -> { try { task.run(); } finally { active.remove(run); } return null; });
        active.put(run,future);
        try { executor.execute(future); return true; }
        catch(java.util.concurrent.RejectedExecutionException e) { active.remove(run); return false; }
    }
    public boolean cancel(String run) {
        // Python 先接收取消墓碑，避免执行请求晚于取消请求到达时重新启动计算。
        try {
            var request=HttpRequest.newBuilder(URI.create(url+"/internal/executions/"+run+"/cancel"))
                .timeout(Duration.ofSeconds(3)).header("X-Agent-Key",key)
                .POST(HttpRequest.BodyPublishers.noBody()).build();
            var response=http.send(request,HttpResponse.BodyHandlers.discarding());
            if(response.statusCode()!=200) return false;
            var future=active.remove(run); if(future!=null) future.cancel(true);
            return true;
        } catch(Exception e) { if(e instanceof InterruptedException) Thread.currentThread().interrupt(); return false; }
    }
    public int activeCount() { return active.size(); }
    public int capacity() { return executor.getMaximumPoolSize(); }
    public static class Failure extends RuntimeException {
        public final int status;
        public Failure(int status) { super("Agent HTTP "+status); this.status=status; }
    }
    public boolean healthy() {
        try { return http.send(HttpRequest.newBuilder(URI.create(url+"/health")).timeout(Duration.ofSeconds(2)).GET().build(),HttpResponse.BodyHandlers.discarding()).statusCode()==200; }
        catch(Exception e) { return false; }
    }
    public Map<String,Object> status() {
        try {
            var response=http.send(HttpRequest.newBuilder(URI.create(url+"/health")).timeout(Duration.ofSeconds(2)).GET().build(),HttpResponse.BodyHandlers.ofString());
            if(response.statusCode()!=200) throw new ApiError(503,"Agent 尚未就绪");
            return json.object(response.body());
        } catch(Exception e) { throw new ApiError(503,"Agent 暂不可用"); }
    }
    public Map<String,Object> execute(Map<String,Object> input) throws Exception {
        var request = HttpRequest.newBuilder(URI.create(url+"/internal/execute"))
            .timeout(Duration.ofSeconds(180)).header("Content-Type","application/json")
            .header("X-Agent-Key",key).POST(HttpRequest.BodyPublishers.ofString(json.write(input))).build();
        var response = http.send(request,HttpResponse.BodyHandlers.ofInputStream());
        try(var stream=response.body()) {
            if(response.statusCode()!=200) throw new Failure(response.statusCode());
            byte[] body=stream.readNBytes(4*1024*1024+1);
            if(body.length>4*1024*1024) throw new IllegalStateException("Agent result too large");
            return json.object(new String(body,StandardCharsets.UTF_8));
        }
    }
    @PreDestroy public void close() { executor.shutdownNow(); http.close(); }
}
