package dev.flowpay.support;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
@org.springframework.scheduling.annotation.EnableScheduling
public class SupportApplication {
    public static void main(String[] args) {
        SpringApplication.run(SupportApplication.class, args);
    }
}
