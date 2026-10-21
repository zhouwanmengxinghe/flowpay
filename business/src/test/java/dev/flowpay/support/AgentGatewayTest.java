package dev.flowpay.support;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class AgentGatewayTest {
    @Test void boundedQueueRejectsExcessAndAcceptsAfterDrain() throws Exception {
        var gateway=new AgentGateway(new Json(new ObjectMapper()),"http://127.0.0.1:1","test-internal-key",1,1);
        var running=new CountDownLatch(1); var release=new CountDownLatch(1); var completed=new CountDownLatch(1);
        try {
            gateway.submit(()->{running.countDown(); try{release.await();}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            assertTrue(running.await(2,TimeUnit.SECONDS));
            gateway.submit(completed::countDown);
            assertThrows(RejectedExecutionException.class,()->gateway.submit(()->{}));
            release.countDown();
            assertTrue(completed.await(2,TimeUnit.SECONDS));
            var accepted=new CountDownLatch(1);
            gateway.submit(accepted::countDown);
            assertTrue(accepted.await(2,TimeUnit.SECONDS));
        } finally { release.countDown(); gateway.close(); }
    }
}
