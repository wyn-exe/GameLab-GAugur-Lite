#include <iostream>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>
#include <X11/extensions/XTest.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <cstring>
#include <sstream>
#include <thread>
#include <vector>
#include <unistd.h>

#define SERVER_PORT 8888

std::string getIPv4Address()
{
    std::string ipAddress;
    ipAddress = "10.242.96.194";
    return ipAddress;
}

void relative_mouse_move(Display* display, int x, int y)
{
    XTestFakeRelativeMotionEvent(display, x, y, 0);
    XFlush(display);
}

void mouse_move(Display* display, int x, int y)
{
    XTestFakeMotionEvent(display, 0, x, y, 0);
    XFlush(display);
}

void button_press(Display* display, unsigned int button)
{
    XTestFakeButtonEvent(display, button, True, 0);
    XFlush(display);
}

void button_release(Display* display, unsigned int button)
{
    XTestFakeButtonEvent(display, button, False, 0);
    XFlush(display);
}

void key_press(Display* display, unsigned int keycode)
{
    XTestFakeKeyEvent(display, keycode, True, 0);
    XFlush(display);
}

void key_release(Display* display, unsigned int keycode)
{
    XTestFakeKeyEvent(display, keycode, False, 0);
    XFlush(display);
}

void handleClient(int clientSockfd)
{
    Display* display = XOpenDisplay(nullptr);
    Window root = DefaultRootWindow(display);

    // Receive the client screen size
    int clientScreenWidth;
    int clientScreenHeight;
    ssize_t bytesReceived = recv(clientSockfd, &clientScreenWidth, sizeof(clientScreenWidth), 0);
    if (bytesReceived <= 0)
    {
        std::cerr << "Failed to receive client screen width" << std::endl;
        close(clientSockfd);
        return;
    }

    bytesReceived = recv(clientSockfd, &clientScreenHeight, sizeof(clientScreenHeight), 0);
    if (bytesReceived <= 0)
    {
        std::cerr << "Failed to receive client screen height" << std::endl;
        close(clientSockfd);
        return;
    }

    // Retrieve the server screen size
    int serverScreenWidth = XDisplayWidth(display, DefaultScreen(display));
    int serverScreenHeight = XDisplayHeight(display, DefaultScreen(display));

    // Calculate the ratio for scaling mouse movements
    double widthRatio = static_cast<double>(serverScreenWidth) / static_cast<double>(clientScreenWidth);
    double heightRatio = static_cast<double>(serverScreenHeight) / static_cast<double>(clientScreenHeight);

    std::cout << "Client screen size: " << clientScreenWidth << "x" << clientScreenHeight << std::endl;
    std::cout << "Server screen size: " << serverScreenWidth << "x" << serverScreenHeight << std::endl;
    std::cout << "Mouse movement ratio: " << widthRatio << ":" << heightRatio << std::endl;

    int scaledMouseX, scaledMouseY; 

    while (true)
    {
        XEvent event;
        ssize_t bytesRead = recv(clientSockfd, &event, sizeof(event), 0);
        if (bytesRead <= 0)
        {
            break;
        }

        switch (event.type)
        {
            case MotionNotify:
                // Scale the mouse coordinates based on the client and server screen sizes
                scaledMouseX = event.xmotion.x * widthRatio;
                scaledMouseY = event.xmotion.y * heightRatio;

                std::cout << "Mouse move: x=" << scaledMouseX << ", y=" << scaledMouseY << std::endl;
                mouse_move(display, scaledMouseX, scaledMouseY);
                break;
            case ButtonPress:
                std::cout << "Button press: " << event.xbutton.button << std::endl;
                button_press(display, event.xbutton.button);
                break;
            case ButtonRelease:
                std::cout << "Button release: " << event.xbutton.button << std::endl;
                button_release(display, event.xbutton.button);
                break;
            case KeyPress:
                std::cout << "Key press: keycode=" << event.xkey.keycode << ", state=" << event.xkey.state << std::endl;
                key_press(display, event.xkey.keycode);
                break;
            case KeyRelease:
                std::cout << "Key release: keycode=" << event.xkey.keycode << ", state=" << event.xkey.state << std::endl;
                key_release(display, event.xkey.keycode);
                break;
            default:
                break;
        }

        // Repeat the received event to simulate the action on the server machine
        XSendEvent(display, root, True, 0, &event);
        XFlush(display);
    }

    XCloseDisplay(display);

    // Indicate that the client is disconnected
    std::cout << "Client disconnected" << std::endl;
}

int main()
{
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (sockfd < 0)
    {
        std::cerr << "Failed to create socket" << std::endl;
        return 1;
    }

    sockaddr_in serverAddr{};
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_addr.s_addr = INADDR_ANY;
    serverAddr.sin_port = htons(SERVER_PORT);

    if (bind(sockfd, reinterpret_cast<sockaddr*>(&serverAddr), sizeof(serverAddr)) < 0)
    {
        std::cerr << "Failed to bind socket" << std::endl;
        close(sockfd);
        return 1;
    }

    if (listen(sockfd, 1) < 0)
    {
        std::cerr << "Failed to listen on socket" << std::endl;
        close(sockfd);
        return 1;
    }

    std::cout << "Server listening on " << getIPv4Address() << ":" << SERVER_PORT << std::endl;

    std::vector<std::thread> clientThreads;

    while (true)
    {
        // Accept a client connection
        int clientSockfd = accept(sockfd, nullptr, nullptr);
        if (clientSockfd < 0)
        {
            std::cerr << "Failed to accept client connection" << std::endl;
            close(sockfd);
            return 1;
        }

        std::cout << "Client connected. Receiving events..." << std::endl;

        // Handle client connection in a separate thread
        std::thread clientThread(handleClient, clientSockfd);
        clientThreads.push_back(std::move(clientThread));
    }

    // Join all client threads
    for (auto& clientThread : clientThreads)
    {
        if (clientThread.joinable())
        {
            clientThread.join();
        }
    }

    return 0;
}
