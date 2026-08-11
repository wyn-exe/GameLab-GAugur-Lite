#include <iostream>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <cstring>
#include <unistd.h>
#include <sstream>

#define SERVER_PORT 8888

std::string getIPv4Address()
{
    std::string ifconfigCmd = "ifconfig";
    std::string ifconfigResult;

    // Execute ifconfig command and capture the output
    FILE* pipe = popen(ifconfigCmd.c_str(), "r");
    if (pipe)
    {
        char buffer[128];
        while (!feof(pipe))
        {
            if (fgets(buffer, 128, pipe) != NULL)
            {
                ifconfigResult += buffer;
            }
        }
        pclose(pipe);
    }

    // Parse the output to get the IPv4 address
    std::string ipAddress;
    std::istringstream stream(ifconfigResult);
    std::string line;
    while (std::getline(stream, line))
    {
        if (line.find("inet ") != std::string::npos)
        {
            std::istringstream lineStream(line);
            std::string word;
            lineStream >> word; // Skip the "inet" keyword
            lineStream >> ipAddress;
            break;
        }
    }

    return ipAddress;
}

int main()
{
    Display* display = XOpenDisplay(nullptr);
    Window root = DefaultRootWindow(display);

    XEvent event;

//    std::string defaultIP = getIPv4Address();
    std::string defaultIP = "10.242.96.194";
    std::cout << "Default IP: " << defaultIP << std::endl;
    std::cout << "Default Port: " << SERVER_PORT << std::endl;

    std::string serverIP;
    unsigned int serverPort;

//    std::cout << "Enter the server IP (press enter to use default IP): ";
//    std::getline(std::cin, serverIP);
//    if (serverIP.empty())
//        serverIP = defaultIP;

//    std::cout << "Enter the server port (press enter to use default port): ";
//    std::string portStr;
//    std::getline(std::cin, portStr);
//    if (portStr.empty())
//        serverPort = SERVER_PORT;
//    else
//        serverPort = std::stoi(portStr);

    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    if (sockfd < 0)
    {
        std::cerr << "Failed to create socket" << std::endl;
        return 1;
    }

    sockaddr_in serverAddr{};
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_addr.s_addr = inet_addr(defaultIP.c_str());
    serverAddr.sin_port = htons(SERVER_PORT);

    if (connect(sockfd, reinterpret_cast<sockaddr*>(&serverAddr), sizeof(serverAddr)) < 0)
    {
        std::cerr << "Failed to connect to the server" << std::endl;
        close(sockfd);
        return 1;
    }

    // Retrieve the client screen size
    int screenWidth = XDisplayWidth(display, DefaultScreen(display));
    int screenHeight = XDisplayHeight(display, DefaultScreen(display));

    // Send the client screen size to the server
    ssize_t bytesSent = send(sockfd, &screenWidth, sizeof(screenWidth), 0);
    if (bytesSent <= 0)
    {
        std::cerr << "Failed to send screen width to the server" << std::endl;
        close(sockfd);
        return 1;
    }

    bytesSent = send(sockfd, &screenHeight, sizeof(screenHeight), 0);
    if (bytesSent <= 0)
    {
        std::cerr << "Failed to send screen height to the server" << std::endl;
        close(sockfd);
        return 1;
    }

    XGrabPointer(display, root, False, PointerMotionMask | ButtonPressMask | ButtonReleaseMask | ButtonMotionMask | Button4MotionMask | Button5MotionMask | Button4Mask | Button5Mask, GrabModeAsync, GrabModeAsync, None, None, CurrentTime);
    XGrabKeyboard(display, root, False, GrabModeAsync, GrabModeAsync, CurrentTime);

    while (true)
    {
        XNextEvent(display, &event);

        if (event.type == KeyPress && XLookupKeysym(&event.xkey, 0) == XK_Escape)
        {
            std::cout << "Esc key pressed. Closing connection..." << std::endl;
            break;
        }

        ssize_t bytesSent = send(sockfd, &event, sizeof(event), 0);
        if (bytesSent <= 0)
        {
            std::cerr << "Failed to send event to the server" << std::endl;
            break;
        }
    }

    XUngrabPointer(display, CurrentTime);
    XUngrabKeyboard(display, CurrentTime);
    XCloseDisplay(display);
    close(sockfd);

    return 0;
}
